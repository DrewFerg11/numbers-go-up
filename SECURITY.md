# Security Policy

## Scope

`numbers-go-up` is a self-hosted service intended to run on a **trusted LAN**.
It ships **no authentication** — that is a deliberate design decision, not an
oversight, and it is documented in `docs/design.md` → Exposure.

This means: **an unauthenticated endpoint reachable by anyone on the network
that can reach it is expected behaviour, not a vulnerability.** If you want
access from outside your home, use a VPN (Tailscale, WireGuard). Publishing the
dashboard to the internet is not a supported deployment.

Reports that *are* in scope:

- Remote code execution, path traversal, or SQL injection through any input the
  service accepts — API query parameters, `config.yaml`, or plugin responses
- A crafted upstream response causing something worse than a failed poll
- Secrets leaking into logs, API responses, or the container image
- The plugin loader executing code from somewhere it shouldn't
- Container escape, or the image running as root when it shouldn't (it runs as
  UID 1000)
- Dependency vulnerabilities that are actually reachable from this code

## Reporting

**Please report privately.** Use GitHub's
[private vulnerability reporting](https://github.com/DrewFerg11/numbers-go-up/security/advisories/new)
— not a public issue, not a pull request.

This is a hobby project maintained by one person. Expect an acknowledgement
within about a week, and please allow a reasonable window before disclosing.

## Supported versions

The latest release only. There are no maintained release branches.

## What this project does with credentials

Nothing — by design. No plugin requires a login, cookie, or session token, and
none ever will (`docs/design.md` → Responsible Use). Plugins read public data
about your own accounts. If a plugin ever asks you for a password, that is the
bug.
