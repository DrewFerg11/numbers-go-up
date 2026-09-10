# Contributing

Thanks for looking. This is a small, opinionated hobby project — reading
`docs/` first will save you time.

## Read the design record first

> **Note:** the design record is being prepared and lands in `docs/` shortly.
> Until it does, the summaries below and this file are the written rules.

`docs/` is authoritative and it is **not** a stale copy of somebody's notes.
It's where the reasoning lives:

| Question | Doc |
|---|---|
| Why this architecture? Plugin contract? API spec? Responsible Use rules? | `docs/design.md` |
| Why SQLite? Schema, migrations, write policy, retention | `docs/database.md` |
| What gets built, in what order, and how do we know it works? | `docs/implementation-guide.md` |
| What else exists out there? Which repos are upstream canaries? | `docs/research.md` |

Some decisions are closed — SQLite over a real TSDB, one container instead of
poller + TSDB + Grafana, MQTT discovery over REST sensors, store-on-change
writes, GHCR as the canonical registry, and the platform-neutral name. Each was
made with evidence that's written down. Reopening one needs new information,
not a preference.

**If a decision changes, the doc changes in the same PR.** Not a comment on the
issue, not a follow-up. Otherwise the docs rot and nobody trusts them.

## Licensing — the one hard rule

This project is MIT. Contributions must be **original work or derived from a
permissively licensed source** (MIT, BSD, Apache-2.0), with the source credited.

**Do not copy AGPL-licensed code into this project.** Not a snippet, not a
"just the parsing bit." AGPL is copyleft: a derived file would relicense the
entire project, which is not a call any single PR gets to make. This matters
concretely here — some of the most useful prior art for the unofficial sources
is AGPL-3.0 (this is recorded in the research notes). **Read those repos to understand the
technique, then write your own implementation.** PRs with AGPL-derived code
will be closed, however good they are.

By contributing you agree your work is licensed under the MIT License.

## Writing a plugin

A new data source should be **one file** in `plugins/` and no changes anywhere
else. If you find yourself editing the scheduler or storage to make a source
work, open an issue — that's a gap in the plugin contract worth discussing
before you write code.

The contract is in `docs/design.md` → Plugin System. Every plugin must:

- **Ship disabled and be inert by default.** No default user IDs, handles, or
  repos in the image. A fresh install makes **zero** outbound requests until
  someone configures one. If the config key is missing, make no request at all
  — don't fetch and discard.
- **Use the shared client from `http.py`.** It sets the honest project
  User-Agent and handles 429 / `Retry-After` centrally. Plugins never build
  their own client, and never spoof a browser UA. (TikTok is the one documented
  exception — its profile page returns nothing usable without a browser UA and
  `Accept-Language`. It says so in a comment, and it stays opt-in.)
- **Be polite.** One request per poll. 30-minute default interval, never below
  5 minutes. No proxy rotation, no CAPTCHA solving, no auth bypass, no cookie
  or session reuse.
- **Read public data about the user's own accounts.** No plugin reads another
  user's private or authenticated data, and none require credentials.
- **Fail as a failed poll, never a crash.** Any non-200, any missing key, any
  shape change is one `plugin_runs` row with `status=error` and a bounded
  traceback tail. The service stays up. Other plugins keep running.
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

- A **data source stopped working** → the "Broken data source" template. This is
  expected and routine; every source except GitHub is unofficial. Please verify
  from your own connection first — the daily canary runs from Azure IPs and can
  get 403s that don't reproduce at home.
- **The service itself misbehaves** → the bug template.
- **A security issue** → privately, per [`SECURITY.md`](SECURITY.md).
