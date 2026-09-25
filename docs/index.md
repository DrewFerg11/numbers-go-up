# numbers-go-up

A self-hosted, plugin-based tracker for the counters you care about — with
history, rate-of-change, a REST API, and Home Assistant integration.

![The numbers-go-up dashboard: pinned metrics, a watchlist with sparklines, and plugin health](assets/dashboard.png)

Most sites show you a number today and no memory of what it was last month.
numbers-go-up runs on your own hardware, checks those numbers on a schedule,
and keeps the history — so you can ask what changed, how fast, and since when.

Want to click around first? A [live demo](demo.md) with sample data is on
the way.

## What you get

- **One container.** A web service, a scheduler, and a SQLite file. No
  time-series database, no Grafana, no message broker to look after.
- **Plugins.** A data source is one Python file. The built-in ones are listed
  under [Plugins](plugins/index.md); writing your own is the point.
- **Small on disk.** A sample is recorded only when a value actually changes,
  with a daily heartbeat so gaps stay meaningful.
- **A dashboard and a REST API.** A watchlist view of every counter, and an
  API with interactive docs served by the container itself.
- **Home Assistant native.** MQTT discovery creates the entities for you, with
  the right `state_class` for long-term statistics.
- **Your data stays yours.** It's a file on your disk. Back it up by copying
  it.

Nothing is tracked until you ask: a fresh install makes zero outbound
requests to any platform until you enable a plugin.

## Quickstart

```sh
git clone https://github.com/DrewFerg11/numbers-go-up.git
cd numbers-go-up
mkdir -p data config user-plugins
docker compose pull && docker compose up -d
curl localhost:8080/health
```

Then open `http://localhost:8080`. The full walkthrough — including the one
deployment mistake that can corrupt the database — is on the
[Install](getting-started/install.md) page.
