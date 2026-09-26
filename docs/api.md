# API reference

Every running instance exposes a small, read-only REST API. This page is
generated from the app's own OpenAPI schema on every deploy, so it can never
drift from what a running instance actually serves.

## Auth

None. There is no login, token or API key. Anyone who can reach the port can
read every endpoint below. Put the app behind a reverse proxy or your own
network controls if that is not what you want.

## Rate limits

None. The app does not throttle requests; it relies on the reverse proxy or
network in front of it for that.

## The `metric` key format

Every metric belongs to a plugin and is addressed as `<plugin-name>` for a
plugin's single default series, or `<plugin-name>.<sub-key>` for a plugin
that reports more than one series (per-model counters, for example). The
same key is what appears in the dashboard's URL as `/m/<key>` and in
`/api/stats/history` and `/api/stats/delta`'s `metric` query parameter.

## The `range` vocabulary

`/api/stats/history` takes a `range` query parameter from a fixed vocabulary
-- the same one the dashboard's range switcher uses: `1H`, `6H`, `12H`,
`1D`, `1W`, `1M`, `3M`, `1Y`, and `ALL`. `/api/stats/delta` takes a plain
`hours` window instead (integer hours back from now, capped at 8760 --
366 days). `ALL` has no fixed bound: each series starts at its own first
sample.

## What is not here, on purpose

The dashboard's own routes (`/`, `/m/{key}`, `/api/stats/overview`) are
excluded from this schema. They are the dashboard UI's private backend, not
a public contract, and may change shape or disappear without notice. Do not
build against them.

<div id="redoc-container"></div>
<script src="../assets/redoc.standalone.js"></script>
<script>
  Redoc.init(
    "../assets/openapi.json",
    {},
    document.getElementById("redoc-container")
  );
</script>
