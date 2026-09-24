"""Plugin discovery and the plugin contract.

This package is both the loader (this file) and the home of the built-in
plugins (its sibling ``*.py`` files: ``abacus.py``, ``github.py``,
``makerworld.py``, ``tiktok.py``, ``youtube.py``). Keeping them together
means the Dockerfile's ``COPY numbers_go_up
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
import functools
import importlib.util
import logging
import re
from pathlib import Path
from types import ModuleType
from typing import Any

from numbers_go_up.storage import VALID_KINDS as VALID_METRIC_KINDS

logger = logging.getLogger(__name__)

MIN_POLL_INTERVAL_SECONDS = 300
# Stems that are not lowercase identifiers are rejected in ``_discover_dir``.
_VALID_PLUGIN_NAME = re.compile(r"^[a-z][a-z0-9_]*$")

# A per-plugin cap on distinct pattern-matched keys returned in one poll, so
# a runaway listing (a paging bug, a source that starts returning every
# object in its database) can't create thousands of series -- and, later,
# thousands of Home Assistant entities. Exceeding it is a contract
# violation, not a silent truncation. Plugins may enforce a tighter,
# user-facing cap of their own (e.g. MakerWorld's ``models.max``).
MAX_PATTERN_KEYS_PER_RUN = 500

# A METRICS key may contain at most one ``{placeholder}``, and it must
# occupy a whole dot-separated segment (``makerworld.model.{id}.downloads``,
# never ``makerworld.model{id}.downloads``).
_PATTERN_PLACEHOLDER = re.compile(r"^\{[a-zA-Z_][a-zA-Z0-9_]*\}$")
# What a placeholder may resolve to at runtime -- lowercase, digits,
# underscore, hyphen; Home-Assistant-entity-id-safe. The single source of
# truth for that charset: both _PLACEHOLDER_VALUE (used to sanity-check the
# constant itself) and _pattern_regex's capturing group are built from it,
# so widening it in one place widens matching too.
_PLACEHOLDER_CHARSET = "[a-z0-9_-]+"
_PLACEHOLDER_VALUE = re.compile(f"^{_PLACEHOLDER_CHARSET}$")

# An *exact* (non-pattern) METRICS key gets the same charset discipline as
# a resolved placeholder segment, plus ``.`` as the segment separator.
# Without this, only the prefix ("<plugin>.") is checked, so a key like
# "acme.a b" or "acme.a/b" would pass -- and every metric key is embedded,
# unvalidated, into an MQTT topic (see mqtt.py's _state_topic/_attrs_topic)
# and turned into a Home Assistant object_id, where a stray space, ``/``,
# ``+``, or ``#`` corrupts entity identity or collides with MQTT's own
# wildcard semantics.
_VALID_EXACT_KEY = re.compile(r"^[a-z0-9_.-]+$")


def is_pattern_key(key: str) -> bool:
    """True if ``key`` is a METRICS pattern template (has a ``{placeholder}``)
    rather than a literal, exact metric key."""
    return "{" in key or "}" in key


def _is_valid_pattern_key(key: str) -> bool:
    """True if ``key`` has exactly one ``{placeholder}``, occupying a whole
    dot-separated segment, and no stray braces anywhere else."""
    segments = key.split(".")
    placeholder_segments = [s for s in segments if _PATTERN_PLACEHOLDER.fullmatch(s)]
    if len(placeholder_segments) != 1:
        return False
    return all(
        s in placeholder_segments or ("{" not in s and "}" not in s) for s in segments
    )


@functools.cache
def _pattern_regex(key: str) -> re.Pattern[str]:
    """Compile a pattern template into a regex matching concrete keys.

    Every literal segment is matched verbatim; the one placeholder segment
    becomes a capturing group constrained to ``_PLACEHOLDER_CHARSET``.
    Cached: a plugin's pattern templates are a small, fixed set for the
    life of the process, so compiling each one once (rather than on every
    ``resolve_metric()`` call, potentially thousands of times per poll)
    keeps the poll loop and the deactivation sweep O(keys) instead of
    O(keys x recompiles).
    """
    parts = [
        f"({_PLACEHOLDER_CHARSET})"
        if _PATTERN_PLACEHOLDER.fullmatch(segment)
        else re.escape(segment)
        for segment in key.split(".")
    ]
    return re.compile("^" + r"\.".join(parts) + "$")


def _patterns_overlap(a: str, b: str) -> bool:
    """True if some concrete key could match both single-placeholder
    pattern templates ``a`` and ``b``.

    Segment counts must match, and every position must be "compatible":
    equal literals, or at least one side a placeholder (a placeholder's
    ``_PLACEHOLDER_CHARSET`` can match a plugin's own lowercase-word literal
    segments, so ``x.a.{id}.count`` and ``x.{k}.{id}.count`` both match
    ``x.a.5.count`` even though position 1 is a literal on one side and a
    placeholder on the other -- requiring *both* sides to be placeholders
    at every differing position would miss exactly that case).
    """
    segments_a = a.split(".")
    segments_b = b.split(".")
    if len(segments_a) != len(segments_b):
        return False
    return all(
        sa == sb
        or _PATTERN_PLACEHOLDER.fullmatch(sa)
        or _PATTERN_PLACEHOLDER.fullmatch(sb)
        for sa, sb in zip(segments_a, segments_b, strict=True)
    )


def resolve_metric(
    key: str, metrics: dict[str, dict[str, Any]]
) -> tuple[str, dict[str, Any]] | None:
    """Match a poll-returned ``key`` against a plugin's ``METRICS``.

    Returns ``(declared_key, meta)`` for the exact key or the one pattern
    template that matches, else ``None``. An exact key always wins over a
    pattern that happens to also match it. ``kind``/``unit`` in the return
    value always come from ``meta`` -- never from the poll -- since a
    pattern's ``kind`` is what makes it safe against Home Assistant's
    long-term statistics.
    """
    if key in metrics and not is_pattern_key(key):
        return key, metrics[key]

    for template, meta in metrics.items():
        if not is_pattern_key(template):
            continue
        if _pattern_regex(template).fullmatch(key):
            return template, meta

    return None


@dataclasses.dataclass(frozen=True)
class LoadedPlugin:
    """A plugin that passed the contract check and is enabled in config."""

    name: str
    module: ModuleType
    metrics: dict[str, dict[str, Any]]
    interval_seconds: int
    config: dict[str, Any]
    source: str  # "built-in" or "user"


@dataclasses.dataclass(frozen=True)
class DiscoveredPlugin:
    """A contract-valid plugin, whether or not it's enabled in config.

    :func:`discover` returns one of these per plugin file that imports
    cleanly and passes :func:`validate_plugin_contract` -- every consumer
    (the enabled-plugin list, the full name list for ``/api/plugins``)
    derives from this same single pass, so a plugin module is only ever
    imported once per process, not once per consumer.
    """

    name: str
    module: ModuleType
    metrics: dict[str, dict[str, Any]]
    config: dict[str, Any]
    interval_seconds: int
    source: str  # "built-in" or "user"
    enabled: bool


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

    A key may be a pattern template with one ``{placeholder}`` segment
    (``"makerworld.model.{id}.downloads"``); it is validated the same way
    as an exact key, plus the placeholder shape check in
    :func:`_is_valid_pattern_key`. ``kind`` and ``unit`` for a pattern are
    fixed here, at load time, and never overridable per poll -- see
    :func:`resolve_metric`. Two patterns that could both match the same
    concrete key (:func:`_patterns_overlap`) are also rejected here: with
    both declared, which one a returned key resolves to would depend on
    dict order, silently picking a possibly-wrong ``kind`` that is then
    pinned forever.
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
        # A non-str key (an int, a tuple, ...) would crash .startswith()
        # below, and a non-hashable kind (a list, a dict) would crash the
        # VALID_METRIC_KINDS membership test after it -- either one, from a
        # single malformed user plugin, used to take the whole app's
        # startup down with it. Check types before using them.
        if not isinstance(key, str):
            logger.warning(
                "Plugin %s: metric key %r must be a string; skipping",
                name,
                key,
            )
            return None
        if not key.startswith(prefix):
            logger.warning(
                "Plugin %s: metric key %r must start with %r; skipping",
                name,
                key,
                prefix,
            )
            return None
        kind = meta.get("kind") if isinstance(meta, dict) else None
        if not isinstance(kind, str) or kind not in VALID_METRIC_KINDS:
            logger.warning(
                "Plugin %s: metric %r has invalid kind %r; skipping",
                name,
                key,
                kind,
            )
            return None

        # meta is a dict by this point (the kind check above already
        # rejected anything else). label is required and non-empty; unit
        # is required but may be "" -- the template promises both, but
        # nothing previously enforced it.
        label = meta.get("label")
        if not isinstance(label, str) or not label:
            logger.warning(
                "Plugin %s: metric %r has invalid label %r (must be a "
                "non-empty string); skipping",
                name,
                key,
                label,
            )
            return None
        if not isinstance(meta.get("unit"), str):
            logger.warning(
                "Plugin %s: metric %r has invalid unit %r (must be a "
                "string, may be empty); skipping",
                name,
                key,
                meta.get("unit"),
            )
            return None

        if is_pattern_key(key):
            if not _is_valid_pattern_key(key):
                logger.warning(
                    "Plugin %s: pattern metric key %r must have exactly one "
                    "{placeholder} occupying a whole dot-separated segment; "
                    "skipping",
                    name,
                    key,
                )
                return None
        elif not _VALID_EXACT_KEY.fullmatch(key):
            logger.warning(
                "Plugin %s: metric key %r must match %s; skipping",
                name,
                key,
                _VALID_EXACT_KEY.pattern,
            )
            return None

    pattern_keys = [key for key in metrics if is_pattern_key(key)]
    for i, key_a in enumerate(pattern_keys):
        for key_b in pattern_keys[i + 1 :]:
            if _patterns_overlap(key_a, key_b):
                logger.warning(
                    "Plugin %s: pattern metric keys %r and %r overlap -- a "
                    "returned key could match either, with ambiguous "
                    "kind/unit/label; skipping",
                    name,
                    key_a,
                    key_b,
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


def discover(
    config: dict[str, Any], builtin_dir: Path | None = None
) -> list[DiscoveredPlugin]:
    """Discover and validate every plugin, enabled or not, in one pass.

    Discovers built-in plugins (``builtin_dir``, defaulting to this
    package's own directory) plus any in ``config["plugin_dir"]``; a user
    plugin replaces a built-in of the same name. Every plugin file that
    imports cleanly and passes :func:`validate_plugin_contract` is
    returned, with ``.enabled`` reflecting
    ``config["plugins"][name]["enabled"]``.

    This is the single import pass: call it once per process (in
    ``main.lifespan``) and derive every other view -- the enabled-plugin
    list for the scheduler/MQTT/milestones, the full name list for
    ``/api/plugins`` -- from its result, rather than calling this or the
    older :func:`discover_plugins`/:func:`discover_plugin_names` more than
    once. Each additional call re-imports every plugin file from scratch
    (a fresh module object per call), which used to mean split module-level
    state (e.g. a plugin's own in-memory guards) between the scheduler's
    copy and everyone else's, and every contract-violation warning logged
    once per call site instead of once.

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

    discovered: list[DiscoveredPlugin] = []
    for name, (module, source) in sorted(combined.items()):
        # One malformed plugin's contract check must never take down
        # discovery for every other plugin -- validate_plugin_contract
        # type-checks its inputs, but this is a last line of defense
        # against anything it doesn't yet guard.
        try:
            metrics = validate_plugin_contract(name, module)
        except Exception:
            logger.exception(
                "Plugin %s: contract check raised unexpectedly; skipping", name
            )
            continue
        if metrics is None:
            continue

        plugin_config = plugins_config.get(name) or {}
        if not isinstance(plugin_config, dict):
            logger.warning(
                "Plugin %s: config entry must be a mapping; "
                "treating plugin as disabled",
                name,
            )
            plugin_config = {}

        enabled = bool(plugin_config.get("enabled"))

        interval = (
            default_interval
            if _is_valid_interval(default_interval)
            else (MIN_POLL_INTERVAL_SECONDS)
        )
        if enabled:
            # Explicit ``is not None`` checks rather than an ``or`` chain,
            # so a configured falsy value such as ``poll_interval: 0`` is
            # respected and reaches the floor check and its warning
            # instead of being silently swallowed into the module or
            # default interval.
            interval = plugin_config.get("poll_interval")
            if interval is None:
                interval = getattr(module, "POLL_INTERVAL_SECONDS", None)
            if interval is None:
                interval = default_interval

            # A non-integer interval (e.g. ``"30m"`` — an easy YAML quoting
            # slip) would raise TypeError at the floor comparison below and
            # abort discovery for every other plugin. Booleans are ints in
            # Python, so ``poll_interval: true`` must be excluded
            # explicitly.
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

        discovered.append(
            DiscoveredPlugin(
                name=name,
                module=module,
                metrics=metrics,
                config=plugin_config,
                interval_seconds=interval,
                source=source,
                enabled=enabled,
            )
        )

    return discovered


def discover_plugins(
    config: dict[str, Any], builtin_dir: Path | None = None
) -> list[LoadedPlugin]:
    """Discover, validate, and resolve the interval for every enabled plugin.

    A plugin is scheduled only if ``config["plugins"][name]["enabled"]`` is
    ``True`` — discovered but unconfigured plugins are silently left out,
    per Responsible Use #2 (a fresh install makes zero outbound requests
    until configured).

    A thin filter over :func:`discover`. Prefer calling :func:`discover`
    directly and reusing its result when you also need
    :func:`discover_plugin_names`' view (e.g. ``main.lifespan``) — calling
    both functions separately imports every plugin file twice.
    """
    return [
        LoadedPlugin(
            name=p.name,
            module=p.module,
            metrics=p.metrics,
            interval_seconds=p.interval_seconds,
            config=p.config,
            source=p.source,
        )
        for p in discover(config, builtin_dir)
        if p.enabled
    ]


def discover_plugin_names(
    config: dict[str, Any], builtin_dir: Path | None = None
) -> list[str]:
    """Every contract-valid plugin name, whether or not it's enabled.

    ``/api/plugins`` reports on every plugin the service could run, not
    just the ones currently turned on. A thin filter over :func:`discover`
    — see its docstring about avoiding repeat imports.
    """
    return [p.name for p in discover(config, builtin_dir)]
