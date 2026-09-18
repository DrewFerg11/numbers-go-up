# GitHub Actions workflows

A short map of `.github/workflows/` for anyone touching CI/CD, plus the
reasoning behind a few choices that aren't obvious from the YAML alone.

| Workflow | Trigger | What it does |
|---|---|---|
| `ci-test.yml` | PR, push to `main` | pytest, ruff, coverage |
| `ci-build.yml` | PR, push to `main` | builds the image, runs a smoke test |
| `publish.yml` | push to `main` (path-filtered) | builds and pushes the `edge` + `sha-*` tags to GHCR |
| `release.yml` | `v*.*.*` tag, manual dispatch | builds, tags, and releases a version, gated by the `release` environment |
| `sources-canary.yml` | schedule | probes each plugin's live endpoint, files an issue on breakage |
| `pr-labeler.yml` | PR opened/edited | labels PRs from their Conventional Commit title prefix |

## Multi-arch: native runners, not QEMU

`amd64` and `arm64` build **natively** in parallel (`ubuntu-latest` and
`ubuntu-24.04-arm`, the latter free on public repos), then merge into one
manifest. This avoids QEMU, which is roughly an order of magnitude slower
and far flakier for any compiled wheel.

`linux/arm/v7` (32-bit ARM -- older Raspberry Pis) is the one exception,
built under QEMU on `ubuntu-latest`. There's no free native 32-bit-ARM
runner: `ubuntu-24.04-arm` is aarch64, and Armv9-A silicon (GitHub's free
arm64 runners included) has dropped AArch32 support entirely, so 32-bit ARM
binaries can't execute there at all. QEMU's real cost is compiling C
extensions, not running Python, so `requirements.txt` uses plain `uvicorn`
instead of `uvicorn[standard]` -- the `[standard]` extra's `uvloop` and
`httptools` are the only dependencies without a prebuilt `armv7l` wheel, and
without them the whole dependency tree installs as wheels even under
emulation. Keep it that way; restoring the extra makes `arm/v7` slow again.

## Why GHCR and not Docker Hub

GHCR is the canonical registry: it's free with no pull-rate limit for public
images, ties directly into the repo's permissions (no separate account or
token to manage for the primary path), and `docker/login-action` against it
needs nothing but `secrets.GITHUB_TOKEN`.

**Docker Hub is mirrored, opt-in.** `release.yml` can also push each tagged
release to Docker Hub, for self-hosters who look there first. It's off by
default and enabled by setting the repo variable `DOCKERHUB_IMAGE` (e.g.
`drewferg11/numbers-go-up`) plus the `DOCKERHUB_USERNAME` / `DOCKERHUB_TOKEN`
secrets, scoped to the `release` environment.

The mirror is a manifest copy, not a second build: `merge` already pushes
each architecture to GHCR by digest and runs `docker buildx imagetools
create` to assemble the multi-arch manifest and tags. `imagetools create`
works across registries, so giving it GHCR digests as sources and a
`docker.io/<image>` tag as an additional target copies the same blobs there
— the digest on Docker Hub is byte-identical to GHCR's, and the workflow
asserts that before finishing.

Only `v*.*.*` (and `v*.*.*-rc.*`) releases are mirrored. The `edge` and
`sha-*` tags from `publish.yml` stay GHCR-only, so the Docker Hub tag list
only ever shows real releases, and nobody pulls `edge` by accident from the
registry most people assume is stable. Forks never have the variable set, so
this never activates for them.

If `DOCKERHUB_IMAGE` is set but either secret is missing, `release.yml`
fails fast in the `authorize` job, before anything builds — a half-published
release (GHCR done, Docker Hub silently skipped) is worse than a failed one.
