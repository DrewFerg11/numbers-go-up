# Notice

numbers-go-up is an unofficial, independent project. It is not affiliated
with, endorsed by, or connected to any of the platforms its plugins can read
from — they're referred to here as sources, not partners or integrations.

- **You are responsible for complying with each source's terms of use.**
  Running a plugin against a platform is your decision and your account; this
  project does not review or vouch for any platform's terms on your behalf.
- **Plugins read public data about your own accounts, and never require
  credentials.** No plugin ships that reads another user's private or
  authenticated data, and none ever ask for a password, API key, or session
  token belonging to a platform.
- **Unofficial sources may break or be removed without notice.** Most of the
  plugins here read from endpoints or page structures that aren't a published,
  supported API. When a platform changes shape, the plugin that depended on it
  can stop working, or be removed from this project entirely, at any time and
  without advance warning. That's expected and routine, not a defect.

See [`LICENSE`](LICENSE) for the terms this software itself is distributed
under, and [`CONTRIBUTING.md`](CONTRIBUTING.md) for the rules a plugin has to
meet before it's accepted.

## Third-party software

- **[paho-mqtt](https://pypi.org/project/paho-mqtt/)** (Eclipse Paho MQTT
  Python client), used for the optional Home Assistant MQTT discovery
  integration. Dual-licensed; used here under the
  [Eclipse Distribution License 1.0](https://www.eclipse.org/org/documents/edl-v10.php)
  (BSD-3-Clause).
- **[uPlot](https://github.com/leeoniya/uPlot)** v1.6.31, MIT License,
  Copyright (c) 2022 Leon Sorokin. Vendored at
  `numbers_go_up/static/vendor/uplot/` (license file alongside) for the
  dashboard's big chart. Not loaded from a CDN; served from this repo.

`/docs` and `/redoc` are served offline via
[fastapi-offline](https://github.com/turettn/fastapi_offline) (MIT License),
a regular pip dependency (see `requirements.txt`) that bundles Swagger UI
(Apache License 2.0, Copyright (c) 2016 SmartBear Software) and ReDoc (MIT
License) instead of loading either from a CDN.
