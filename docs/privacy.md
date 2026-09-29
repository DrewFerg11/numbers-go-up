# Privacy

This site has **no analytics**. There is no tracking script, no cookie
banner because there are no cookies to ask about, no telemetry and no
third-party embed. That is a decision, not an omission: numbers-go-up is a
self-hosted tool that sends nothing anywhere unless you tell it to, and its
documentation site should not watch its readers.

- **No third-party requests.** Every page loads its fonts, styles, scripts
  and images from this site alone. The app follows the same rule.
- **Theme choice stays in your browser.** The light/dark toggle is
  remembered in your browser's local storage on your own device; it is never
  sent to a server.
- **The demo is the same.** The [live demo](demo.md) is static files with
  sample data and makes no requests beyond this site.
- **The host keeps its own logs.** The site is served by GitHub Pages, which
  keeps whatever access logs GitHub keeps. Nothing here reads or adds to
  them.

The application itself is covered in the same spirit: a fresh install makes
zero outbound requests until you configure a source. See
[Opt-in and inert by default](plugins/index.md#opt-in-and-inert-by-default) for how sources are opt-in and inert by
default.
