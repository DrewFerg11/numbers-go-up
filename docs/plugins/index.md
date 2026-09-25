# Plugins

Each data source is a plugin: one Python file that the scheduler discovers and
polls on an interval.

**Every plugin is opt-in and inert by default.** A fresh install makes zero
outbound requests to any platform until you configure one, and no user IDs or
handles are baked into the image.

Built-in plugins:

- [MakerWorld](makerworld.md)
- [GitHub](github.md)
- [YouTube](youtube.md)

Want a source that isn't here? See [Writing a plugin](writing-a-plugin.md).

!!! note "More to come"
    Per-plugin pages are written in [#106](https://github.com/DrewFerg11/numbers-go-up/issues/106).
