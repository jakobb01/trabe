# Validation

## Reproduce the checks

Install development dependencies and run the commands in
[CONTRIBUTING.md](../CONTRIBUTING.md). Unit and HTTP integration tests need no real
credentials. HTTP fixtures enforce provider pacing and exercise real urllib
requests over loopback, Telegram authorization, commands, background scans,
cached quotes, settings and durable state through a process restart.

The container script runs the suites in the production image with no external
networking, a read-only root filesystem, no capabilities and a 256 MB memory limit.
For isolated Podman storage, set `CHECK_RUNTIME_ROOT=/tmp/stockpulse-check`.

## GitHub preparation: 2026-11-03

A clean copy containing only the 44 files selected by Git's ignore rules passed
all 88 unit/deployment/tooling tests. The production image built from that copy
and passed the same 88 tests plus both HTTP integration tests under the runtime
restrictions above. Ruff lint/formatting, shell syntax and the Podman 4.3 Quadlet
parser check also passed.

The candidate files were checked for configured credentials and personal host
details. Private environment files, local state, archives and development tools
are excluded. Both environment templates and `deploy/data/.keep` are included.
SSH configuration now comes from ignored `.env.deploy` or environment overrides;
the application runtime and deployed Pi were not changed by this preparation.

## Verified baseline: 0.2.1

The initial rollout and follow-up corrections were verified on 2026-11-02:

| Check | Result |
| --- | --- |
| Python unit, deployment and tooling suites | 83 passed |
| Ruff lint and formatting | Passed |
| amd64 production-image HTTP integration | 2 passed |
| ARMv7 application suites under QEMU | 66 passed |
| ARMv7 production-image HTTP integration under QEMU | 2 passed |
| Actual Podman 4.3 parser accepts generated start/stop options | Passed |
| Stale CID cleanup over three simulated restart cycles | Passed |
| Physical ARMv7 Pi deploy, readiness and real market quote | Passed |
| Physical Pi service restart preserves watchlist/settings | Passed |
| Final Telegram delivery and zero published ports | Passed |

The ARM application suites exclude deployment/tool-preparation subprocess tests;
those run natively. Earlier 0.2.0 checks also verified Python 3.11 compatibility,
wheel installation, SQLite schema 1-to-2 migration, image/source agreement and
the runtime restrictions below. Those records are historical; run the current
suites when changing the code. GitHub Actions has not yet run for this repository.

## Runtime and deployment checks

Verified container restrictions include UID/GID 1000, zero effective capabilities,
`NoNewPrivs=1`, a read-only application filesystem, a 256 MB memory limit and no
published ports. Private environment files are absent from the application image.
Both image architectures use the same pinned multi-architecture Python index.

The physical target runs ARMv7 Raspbian 12 with Podman 4.3.1. Its pinned Debian
Quadlet compatibility binary was checksum-verified and executed against Bookworm
under ARM emulation. The generator requires at most GLIBC 2.34; Bookworm provides
2.36. Generated start/stop options were accepted by the real Podman 4.3 parser.
An intentionally invalid option was rejected as a control.

The first physical rollout exposed a retained Podman CID file during restart.
The Quadlet now clears only its own stale CID file before starting and gives the
process time to finish a Telegram long poll. The deployment confirmation is sent
after readiness, market API, restart persistence and published-port checks pass.
Failed rollouts collect diagnostics before restoring the previous release.

A two-stock scan exposed the provider's rapid-request limit. Version 0.2.1 shares
a private request lock and completion timestamp across clients and processes,
waits 1.1 seconds between uncached requests, and recognizes rate-limit notices
returned with HTTP 200. Cache hits spend no requests. Live diagnosis returned a
valid VST quote; a second immediate request received the explicit rate notice.

## Remaining acceptance

The user confirmed `/status`, `/menu`, watchlist additions and watchlist display on
the physical Pi. The 0.2.1 rollout passed its automated gates. User confirmation of
the repeated GOOGL/VST scan remains pending, as does physical reboot verification
during an appropriate maintenance window.

Local logs, private state snapshots, downloaded tools and image archives stay in
ignored local storage. They are not needed to clone the source or run the tests.
