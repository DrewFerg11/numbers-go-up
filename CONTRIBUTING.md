# Contributing

Thanks for looking. This is a small, opinionated hobby project — this file
and the code comments it points at are the written rules.

## Design record

The design record (why SQLite, why one container, the plugin contract's
full rationale, and so on) is the maintainer's private working notes, not
a tracked part of this repo. What *is* public and authoritative:

- **This file** — the rules below, including the numbered Responsible Use
  and Failure Handling list every code comment cites.
- **Code comments** — the *why* behind a specific piece of code lives next
  to that code, not in a separate doc.
- **Closed decisions**, listed once here so they don't get relitigated in
  every PR: SQLite over a real TSDB, one container instead of poller +
  TSDB + Grafana, MQTT discovery over REST sensors, store-on-change writes,
  GHCR as the canonical registry, and the platform-neutral name. Each was
  made with evidence the maintainer has; reopening one needs new
  information, not a preference.

**If a decision changes, this file (or the relevant code comment) changes
in the same PR.** Not a comment on the issue, not a follow-up. Otherwise
the docs rot and nobody trusts them.

## Project rules

Every `Responsible Use #N` / `Failure Handling #N` comment in the code
cites one of these. Keep the numbering stable — a citation with no
matching rule here is a bug.

**Responsible Use**

1. *(reserved — not yet cited in code)*
2. **Opt-in and inert by default.** No default user IDs, handles, or repos
   ship in the image. A fresh install makes zero outbound requests until
   someone configures a source. If a config key is missing, make no
   request at all — never fetch and discard.
3. **Be polite.** Use the shared client from `http.py` only — no plugin
   builds its own. Send the honest project User-Agent (documented,
   per-plugin exceptions require a comment explaining why: TikTok's
   browser-shaped UA, MakerWorld's model listing, GitHub's release
   paging). A timeout on every request, no cookie or session reuse, no
   automatic retries. One request per *configured subject* per poll —
   not one request per poll; a plugin tracking five repos/channels/
   handles/counters makes five requests, each capped by the plugin's own
   `max` — plus the documented paging exceptions above. 30-minute default
   interval, never below 5 minutes. Jitter only ever delays a request,
   never brings one forward. On a 429 or 403, back off (see Failure
   Handling #4) instead of retrying immediately.

**Failure Handling**

1. **Every poll is wrapped.** Start a `plugin_runs` row, call the
   plugin's `collect()` inside try/except, validate and store what came
   back, and always finish the run row — success or failure. A failing
   plugin never stops another plugin or crashes the service.
2. **A plugin goes unhealthy after 3 consecutive failed polls.** That's
   what turns `/health/plugins` and the dashboard's status dot red;
   tunable via `/health/plugins?failures=N`.
3. *(reserved — not yet cited in code)*
4. **No backoff storms.** An ordinary error doesn't change a plugin's
   polling interval. Only a 429 or 403 backs off, and that backoff is
   capped (see `scheduler.compute_backoff_delay_seconds`) — never an
   unbounded or indefinite retreat.
5. **Store the tail of a traceback, not the head.** A failed poll's
   `plugin_runs.error` keeps the last ~500 characters of the exception,
   so the actually-useful frame (what failed, not the call chain that led
   there) survives truncation.
6. **The container boots and answers `/health` with zero plugins
   configured.** A missing or empty `config.yaml` is a valid starting
   state, not a startup failure.

**Unnumbered rules cited by comment, not by number:**

- Don't flood the log — a plugin logs one warning when it starts failing
  and one line when it recovers, not one line per poll.
- A single failed poll (e.g. a Cloudflare 403 on one paginated request)
  must never retire every series a pattern-key plugin already knows
  about — only an explicit reconciliation after a *successful* poll
  removes a series that's stopped being returned.
- Retries for a stuck delivery (e.g. a milestone webhook) are bounded —
  retried on the next successful poll, dropped after 24 hours of
  failures rather than retried forever.

## Licensing — the one hard rule

This project is MIT. Contributions must be **original work or derived from a
permissively licensed source** (MIT, BSD, Apache-2.0), with the source credited.

**Do not copy AGPL-licensed code into this project.** Not a snippet, not a
"just the parsing bit." AGPL is copyleft: a derived file would relicense the
entire project, which is not a call any single PR gets to make. This matters
concretely here — some of the most useful prior art for the unofficial
sources (MakerWorld, TikTok) is AGPL-3.0. **Read those repos to understand
the technique, then write your own implementation.** PRs with AGPL-derived
code will be closed, however good they are.

By contributing you agree your work is licensed under the MIT License.

## Writing a plugin

A new data source should be **one file** and no changes anywhere else: a
built-in lives in `numbers_go_up/plugins/`, a user plugin in whatever
directory `NGU_PLUGIN_DIR` points at (`./user-plugins` on the host, in the
default compose setup) — there is no top-level `plugins/` directory. If you
find yourself editing the scheduler or storage to make a source work, open
an issue — that's a gap in the plugin contract worth discussing before you
write code.

Copy [`numbers_go_up/plugins/_template.py`](numbers_go_up/plugins/_template.py)
to start; its docstrings and `numbers_go_up/plugins/__init__.py`'s module
docstring are the in-repo contract. Every plugin must also follow the
Responsible Use and Failure Handling rules above, plus:

- **Read public data about the user's own accounts.** No plugin reads
  another user's private or authenticated data, and none require
  credentials.
- **Come with an offline fixture test.** Commit a captured response under
  `tests/fixtures/` with your own IDs scrubbed. That's what CI runs. A live
  test is welcome but must be marked `@pytest.mark.live`, which CI excludes.

Declare `METRICS` honestly: `cumulative` for counters that only rise,
`gauge` for anything that can fall. This is what Home Assistant's `state_class`
is derived from, and getting it wrong corrupts long-term statistics.

## Working on the code

- **All SQL lives in `storage.py`.** No exceptions, no ORM, no queries in route
  handlers or plugins.
- **Migrations are forward-only and never edited once applied.** Add a new
  numbered file.
- **Tests run offline.** `pytest` must pass with no network. If a test needs
  the network it's a live test and CI doesn't run it.
- `ruff` for linting. Keep it clean; CI fails otherwise.

## Pull requests

- One PR per issue, roughly one acceptance-criterion cluster. If it's a week of
  work, it's several PRs.
- **Link the issue** (`Closes #14`). The issue's acceptance criteria are the
  spec the review checks against — that's why they're copied verbatim into the
  issue body.
- `main` is protected: squash merge, one approving review, no force pushes.
- Say what you actually ran to verify it. "Tests pass" isn't verification.

## Reporting things

- A **data source stopped working** → the "Broken data source" template. This
  is expected and routine for MakerWorld and TikTok, the two unofficial
  (scraped) sources — GitHub, YouTube, and Abacus read documented APIs and
  are comparatively stable. Please verify from your own connection first —
  the daily canary runs from Azure IPs and can get 403s that don't
  reproduce at home.
- **The service itself misbehaves** → the bug template.
- **A security issue** → privately, per [`SECURITY.md`](SECURITY.md).
