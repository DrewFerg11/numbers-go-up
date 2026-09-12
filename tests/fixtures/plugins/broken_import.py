"""Fixture plugin that raises during import -- must be logged and skipped
without stopping discovery of other plugins.
"""

raise RuntimeError("this plugin is broken on import")
