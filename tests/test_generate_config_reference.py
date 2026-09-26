"""Offline tests for scripts/generate_config_reference.py (#163)."""

from __future__ import annotations

from scripts.generate_config_reference import generate


def test_every_config_field_has_a_description():
    from numbers_go_up.config import BackupsConfig, Config, PollConfig, StorageConfig

    for model in (Config, PollConfig, StorageConfig, BackupsConfig):
        for name, field in model.model_fields.items():
            assert field.description, f"{model.__name__}.{name} has no description"


def test_generated_page_lists_every_strict_field_and_its_constraint():
    output = generate()

    # Nested (storage.backups.keep_daily) and top-level (poll.jitter_fraction)
    # dotted paths both appear -- confirms the schema walk recurses through
    # $ref'd sub-models rather than stopping at the first level.
    assert "`storage.backups.keep_daily`" in output
    assert "`poll.jitter_fraction`" in output
    assert "`storage.path`" in output

    # A required field (no default) reads as required, not as some other
    # sentinel a template could silently swallow.
    assert "**required**" in output

    # A validator's constraint text made it into the rendered row, not
    # just the bare default -- this is what makes the page more than a
    # dump of config.yaml.example.
    assert "positive integer" in output
    assert "[0, 1)" in output


def test_default_factory_fields_are_not_misreported_as_required():
    # dashboard.pinned and plugins both use default_factory, which has no
    # literal "default" in the JSON schema -- a naive "no default key
    # means required" reading would mislabel both as required.
    output = generate()

    for line in output.splitlines():
        if line.startswith("| `dashboard.pinned`") or line.startswith("| `plugins`"):
            assert "**required**" not in line, line


def test_generation_needs_no_config_file_database_or_network():
    # generate() must be pure: import + schema walk + a from-scratch
    # instance, nothing that touches the filesystem or a socket.
    generate()
    generate()
