"""Plugin discovery and the plugin contract.

This package is both the loader (this file) and the home of the built-in
plugins (its sibling ``*.py`` files, e.g. a future ``makerworld.py``).
Keeping them together means the Dockerfile's ``COPY numbers_go_up
./numbers_go_up`` ships both without a separate top-level ``plugins/``
folder that would otherwise be missing from the image.

User plugins are loaded from ``NGU_PLUGIN_DIR`` (``config["plugin_dir"]``),
which takes precedence over a built-in of the same name. Only ``*.py``
files directly in this package's directory or in ``NGU_PLUGIN_DIR`` are
ever imported — no recursion, no following config-supplied paths beyond
the one plugin dir, no adding directories to ``sys.path``.
``NGU_PLUGIN_DIR`` executes arbitrary code by design; loading by explicit
file path (rather than adding the directory to ``sys.path``) means a user
file named e.g. ``json.py`` can't shadow the standard library for the
whole process.

A plugin's name is its filename stem, and the stem becomes the prefix of
its published metric keys (``<stem>.<metric>``) and a Home-Assistant-facing
entity-id fragment. Stems that are not lowercase identifiers
(``bad-name.py``, ``2024 stats.py``) are logged and skipped at discovery,
never imported.

The loader imports modules but never calls ``collect()`` — importing a
plugin must not make a network request.
"""

from __future__ import annotations

import dataclasses
import importlib.util
import logging
import re
from pathlib import Path
from types import ModuleType
from typing import Any

logger = logging.getLogger(__name__)

MIN_POLL_INTERVAL_SECONDS = 300
VALID_METRIC_KINDS = {"gauge", "cumulative"}
# Stems that are not lowercase identifiers are rejected in ``_discover_dir``.
_VALID_PLUGIN_NAME = re.compile(r"^[a-z][a-z0-9_]*$")


@dataclasses.dataclass(frozen=True)
class LoadedPlugin:
    """A plugin that passed the contract check and is enabled in config."""

    name: str
    module: ModuleType
    metrics: dict[str, dict[str, Any]]
    interval_seconds: int
    config: dict[str, Any]
    source: str  # "built-in" or "user"


def load_plugin_from_path(path: str | Path) -> ModuleType | None:
    """Import one plugin module from an explicit file path.

    Used by discovery, and directly by tests to load fixture plugins whose
    underscore-prefixed filenames normal discovery skips on purpose. A
    module that raises on import is logged and skipped, returning None.
    """
    path = Path(path)
    spec = importlib.util.spec_from_file_location(path.stem, path)
    if spec is None or spec.loader is None:
        logger.warning("Plugin %s: could not build an import spec; skipping", path)
        return None

    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception:
        logger.exception("Plugin %s failed to import; skipping", path.stem)
        return None
    return module


def validate_plugin_contract(
    name: str, module: ModuleType
) -> dict[str, dict[str, Any]] | None:
    """Check a loaded module against the plugin contract.

    Returns its ``METRICS`` dict if the module satisfies the contract
    (has ``collect``, a non-empty ``METRICS`` whose keys are all prefixed
    ``"<name>."`` and whose ``kind`` is valid), else logs why and returns
    None.
    """
    if not callable(getattr(module, "collect", None)):
        logger.warning("Plugin %s: missing collect(); skipping", name)
        return None

    metrics = getattr(module, "METRICS", None)
    if not isinstance(metrics, dict) or not metrics:
        logger.warning("Plugin %s: missing or empty METRICS; skipping", name)
        return None

    prefix = f"{name}."
    for key, meta in metrics.items():
        if not key.startswith(prefix):
            logger.warning(
                "Plugin %s: metric key %r must start with %r; skipping",
                name,
                key,
                prefix,
            )
            return None
        kind = meta.get("kind") if isinstance(meta, dict) else None
        if kind not in VALID_METRIC_KINDS:
            logger.warning(
                "Plugin %s: metric %r has invalid kind %r; skipping",
                name,
                key,
                kind,
            )
            return None

    return metrics


def _discover_dir(directory: Path | None) -> dict[str, ModuleType]:
    """Import every non-underscore, validly-named ``*.py`` file in ``directory``.

    A missing or empty directory is not an error. Filenames starting with
    ``_`` (this package's own ``__init__.py`` and ``_template.py`` included)
    are never imported here, and neither is a stem that is not a lowercase
    identifier (``bad-name.py``, ``2024 stats.py``): the stem becomes the
    plugin name and the prefix of its published metric keys, which must be
    stable and Home-Assistant-safe, so those files are logged and skipped
    without ever being imported.
    """
    found: dict[str, ModuleType] = {}
    if directory is None or not directory.is_dir():
        return found

    for path in sorted(directory.glob("*.py")):
        if path.stem.startswith("_"):
            continue
        if not _VALID_PLUGIN_NAME.fullmatch(path.stem):
            logger.warning(
                "Plugin file %s: name %r is not a valid plugin name "
                "(lowercase letter, then letters, digits, or underscores); skipping",
                path,
                path.stem,
            )
            continue
        module = load_plugin_from_path(path)
        if module is not None:
            found[path.stem] = module
    return found


def _is_valid_interval(value: Any) -> bool:
    """A poll interval must be an int.

    ``bool`` is an ``int`` subclass, but ``poll_interval: true`` is a
    config mistake, not a number of seconds.
    """
    return isinstance(value, int) and not isinstance(value, bool)


def discover_plugins(
    config: dict[str, Any], builtin_dir: Path | None = None
) -> list[LoadedPlugin]:
    """Discover, validate, and resolve the interval for every enabled plugin.

    Discovers built-in plugins (``builtin_dir``, defaulting to this
    package's own directory) plus any in ``config["plugin_dir"]``; a user
    plugin replaces a built-in of the same name. A plugin is scheduled only
    if ``config["plugins"][name]["enabled"]`` is ``True`` — discovered but
    unconfigured plugins are silently left out, per Responsible Use #2 (a
    fresh install makes zero outbound requests until configured).

    ``builtin_dir`` exists so tests can point discovery at a fixture
    directory instead of the real built-in plugins.
    """
    if builtin_dir is None:
        builtin_dir = Path(__file__).parent
    builtin_modules = _discover_dir(builtin_dir)

    user_dir = config.get("plugin_dir")
    user_modules = _discover_dir(Path(user_dir)) if user_dir else {}

    combined: dict[str, tuple[ModuleType, str]] = {
        name: (module, "built-in") for name, module in builtin_modules.items()
    }
    for name, module in user_modules.items():
        if name in builtin_modules:
            logger.warning(
                "User plugin %s (%s) replaces the built-in plugin of the "
                "same name (%s)",
                name,
                user_dir,
                builtin_dir,
            )
        combined[name] = (module, "user")

    plugins_config = config.get("plugins") or {}
    default_interval = (config.get("poll") or {}).get("default_interval", 1800)

    loaded: list[LoadedPlugin] = []
    for name, (module, source) in sorted(combined.items()):
        metrics = validate_plugin_contract(name, module)
        if metrics is None:
            continue

        plugin_config = plugins_config.get(name) or {}
        if not plugin_config.get("enabled"):
            continue

        # Explicit ``is not None`` checks rather than an ``or`` chain, so a
        # configured falsy value such as ``poll_interval: 0`` is respected
        # and reaches the floor check and its warning instead of being
        # silently swallowed into the module or default interval.
        interval = plugin_config.get("poll_interval")
        if interval is None:
            interval = getattr(module, "POLL_INTERVAL_SECONDS", None)
        if interval is None:
            interval = default_interval

        # A non-integer interval (e.g. ``"30m"`` — an easy YAML quoting slip)
        # would raise TypeError at the floor comparison below and abort
        # discovery for every other plugin. Booleans are ints in Python, so
        # ``poll_interval: true`` must be excluded explicitly.
        if not _is_valid_interval(interval):
            fallback = (
                default_interval
                if _is_valid_interval(default_interval)
                else MIN_POLL_INTERVAL_SECONDS
            )
            logger.warning(
                "Plugin %s: poll interval %r is not an integer; using %ss instead",
                name,
                interval,
                fallback,
            )
            interval = fallback

        if interval < MIN_POLL_INTERVAL_SECONDS:
            logger.warning(
                "Plugin %s: poll interval %ss is below the %ss floor; raising it",
                name,
                interval,
                MIN_POLL_INTERVAL_SECONDS,
            )
            interval = MIN_POLL_INTERVAL_SECONDS

        loaded.append(
            LoadedPlugin(
                name=name,
                module=module,
                metrics=metrics,
                interval_seconds=interval,
                config=plugin_config,
                source=source,
            )
        )

    return loaded
