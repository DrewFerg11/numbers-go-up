# Plugins

Each data source is a plugin: one Python file that the scheduler discovers and
polls on an interval.

**Every plugin is opt-in and inert by default.** A fresh install makes zero
outbound requests to any platform until you configure one, and no user IDs or
handles are baked into the image.

The image ships more built-in plugins than are documented here so far —
the full set is in
[`numbers_go_up/plugins/`](https://github.com/DrewFerg11/numbers-go-up/tree/main/numbers_go_up/plugins).
Pages so far:

- [MakerWorld](makerworld.md)
- [GitHub](github.md)
- [YouTube](youtube.md)

Want a source that isn't here? See [Writing a plugin](writing-a-plugin.md).

!!! note "More to come"
    Per-plugin pages are tracked in [#106](https://github.com/DrewFerg11/numbers-go-up/issues/106).
