Closes #

## What this changes

<!-- One paragraph. The issue carries the acceptance criteria; don't restate them here. -->

## How it was verified

<!-- The commands you actually ran, and what they printed. "Tests pass" is not verification. -->

## Checklist

- [ ] Linked to an issue above — the reviewer reads its acceptance criteria as the spec
- [ ] Tests are **offline**: no network access in anything CI runs. Live probes are marked `@pytest.mark.live` and excluded by `-m "not live"`
- [ ] `ruff` clean, `pytest` green
- [ ] **If a decision changed, `README.md`/`CONTRIBUTING.md` (or the relevant code comment) changed in this same PR** — not just the issue thread
- [ ] No secrets, tokens, cookies, personal user IDs, or handles committed — including in test fixtures
- [ ] All SQL lives in `storage.py`

### If this touches a plugin or an outbound request

- [ ] Plugin is **opt-in and inert by default** — no default user ID, handle, or repo ships in the image
- [ ] Uses the shared client from `http.py`; the plugin does not construct its own
- [ ] Poll interval is >= 5 minutes; jitter and 429/`Retry-After` handling are not bypassed
- [ ] Offline fixture test committed alongside it
- [ ] Nothing here reads another user's private or authenticated data

### Licensing

- [ ] All code here is original or copied from a **permissively licensed** source (MIT/BSD/Apache-2.0), and any such source is credited
- [ ] **No AGPL-derived code.** It would relicense this entire project. This is a hard rule, not a preference
