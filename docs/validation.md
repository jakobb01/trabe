# Validation

## Current testing policy

Routine development now uses focused unit tests and relevant lint/syntax checks.
Default CI runs the unit suite once on Python 3.13. HTTP/WebSocket integration,
container checks, Python 3.11 compatibility and storage soaks are manual options.
See [CONTRIBUTING.md](../CONTRIBUTING.md#testing-policy). The extensive 0.3 results
below are historical release evidence, not a checklist to repeat per feature.

## Reproduce optional checks

Install development dependencies and run the commands in
[CONTRIBUTING.md](../CONTRIBUTING.md). Unit and HTTP integration tests need no real
credentials. HTTP/WebSocket fixtures enforce provider pacing and exercise real urllib
requests over loopback, Telegram authorization, commands, background scans,
cached quotes, settings and durable state through a process restart.

The container script checks the restricted runtime by default. Enable
`CHECK_EXTENDED=1` for the integration fixture or `CHECK_SOAK=1` for the storage
soak. These run in the production image with no external networking, a read-only
root filesystem, no capabilities and a 256 MB memory limit.
For isolated Podman storage, set `CHECK_RUNTIME_ROOT=/tmp/trabe-check`.

## 0.7 candidate (2026-11-04)

Milestones 0.5–0.7 are implemented against [the checklist](../todo05-07.md).
Research agents recorded source access/constraints before separate coding agents
implemented earnings, insiders and news. The final integration/review stayed with
one agent. The Pi remains on 0.4.0; no candidate deployment or real Telegram
polling/messages were performed during this work.

| Evidence | Result and scope |
| --- | --- |
| Assembled baseline unit suite | 176 tests passed at the earlier integration checkpoint, in 15.991 seconds. Not rerun after focused review fixes. |
| Earnings | 13 focused tests passed, including fiscal identity, revisions/conflicts, cached Alpha expansion, receipt timestamps and after-close uncertainty. |
| Insider purchases | 13 focused tests passed, including role/size/derivative matching, amendments, first-run seed, receipt time and durable claims. |
| News | 12 focused tests passed, including RSS fallback, cross-symbol deduplication, frozen as-of data, limits and extract/sentiment labels. |
| Shared transport/runtime units | 12 focused tests passed after final fixes, including Alpha quota/invalid CSV handling, atomic outbox claims, schema 4→5 preservation, cleanup and morning scheduling. One existing retention/preservation case also passed. |
| Combined HTTP runtime | One isolated walkthrough passed in 12.361 seconds: real urllib transport, Telegram authorization/commands, responsive status during a blocked fetch, combined manual digest, one automatic earnings warning, pause/manual views and restart without duplicate warnings or cache refetch. |

The HTTP walkthrough used fixture credentials, a temporary SQLite database and
loopback endpoints inside an existing tools container with external networking
disabled, read-only source/root filesystem, no capabilities and a 256 MB limit.
Its first attempt exposed a fixture URL-routing mistake; correcting the fixture
produced the passing run. No application failure was hidden. Final schedule and
retention fixes were checked by their affected unit cases, reusing the passing
manual-runtime walkthrough for unchanged paths.

Live adapters were checked separately with the existing keys and a private
temporary database, never through Telegram or the Pi. GOOGL/VST yielded two
earnings records, three qualified insider records and forty news extracts, with
no source errors. Two daily news signals were saved. Alpha CSV contained VST;
Yahoo RSS returned 19 items. That run used six Finnhub calls, one Alpha call and
enqueued zero messages. Sanitized local evidence is in ignored
`.local/validation/insights-live.json`. This proves observed access for the
current account, not future provider availability or complete market coverage.

Ruff lint/format checks cover changed Python files; whitespace and documentation
links are reviewed. No new image build, ARM/QEMU run, storage soak or repeated
broad suite was required: runtime dependencies and deployment configuration are
unchanged. On deployment, use the installer's existing build/readiness/delivery
checks and [one Telegram walkthrough](release-0.7.md#one-walkthrough-after-deployment).

To repeat only the new HTTP boundary check when a change warrants it:

```sh
python3 -m unittest discover -s tests -p integration_signals.py -v
```

## Position release: 0.4 review (2026-11-03)

Reviewed the implementation against `todo04.md`: EUR/USD defaults and explicit
trade currencies, decimal weighted-average cost, partial sales and corrections,
historical FX provenance, converted valuations, held-symbol coverage, daily
progress and ledger-preserving retention are implemented.

The focused checks passed: 2 ledger tests, 14 portfolio/FX tests and 31 core bot
tests (47 total). Ruff lint/format checks passed for the changed Python files;
the changed installer passed shell syntax and whitespace checks. The live
[Frankfurter v1 endpoint](https://frankfurter.dev/v1/) returned a usable EUR/USD
reference rate and the prior Friday's rate for a Saturday historical request.
No broad integration, container or ARM
soak was repeated for this review.

Review fixes prevent failed old FX dates from starving later backfills and
recheck pause before the next FX request in a batch. Portfolio and Currency
buttons were added to the persistent Telegram keyboard.

The owner rebooted the Pi after an initial SSH connection refusal. Its 0.3 service
returned with the existing settings and watchlist intact. The subsequent 0.4
rollout completed through the usual script; the owner walkthrough is documented
in [the release notes](release-0.4.md). This remains the deployed baseline while
the 0.7 candidate awaits rollout.

## Intraday release: 0.3 (2026-11-03)

The release passes 118 unit/deployment/tooling tests natively on Python 3.13 and
in the production container. All three HTTP/WebSocket runtime tests pass in the
native image and the ARMv7 production image under QEMU. These use fixture
credentials and isolated databases, including on the Pi; they never send synthetic
financial alerts to the real owner.

The streaming fixture verifies an intra-minute breach followed by recovery,
exactly one durable event, rapid remove/re-add, watchlist reconciliation,
reconnect and malformed frames, a single active socket, owner authorization,
pause across restart, local controls without provider calls, and resume without
another session alert. Unit checks cover transactional rollback/concurrency,
mixed feeds/currencies/baselines, source age, no-data/partial rollups, holidays,
early close/DST, calendar expiry, month-end/leap retention, protected backups,
low storage and a busy WAL reader followed by successful checkpoint recovery.

A populated schema-2 database was upgraded, restored from its online snapshot,
and reopened by the previous 0.2.2 image. Watchlist, settings, request usage and a
pending reply survived. The new `backup-prune` CLI preserved the restored schema
without opening/migrating it. Simulated installer failures also exercise rollback.

The eight-session, 50-stock production-code soak processed 156,000 observations
under a 256 MiB container memory limit. Native peak RSS was 30,412,800 bytes;
the sampled DB/WAL/shared-memory peak was 1,970,176 bytes. The ARMv7 QEMU run
processed the same eight sessions with peak RSS 62,959,616 bytes including the
emulator. Finalized sessions left no minute
rows, retained 400 daily rows, reused pages and then removed all expired history.
This is an eight-session soak, separate from the synthetic three-month capacity
model; container images, host logs and backups are excluded from those numbers.

The installed 0.3 wheel includes the calendar JSON and works with the pinned
`websocket-client==1.9.2` wheel. Runtime staging verifies both the downloaded wheel
SHA256 and the extracted tree. ARM builds execute no target-CPU pip/apt step.
Ruff, shell syntax, whitespace and Podman 4.3 Quadlet parser checks pass. The
Git candidate files contain none of the configured credential values.

Real free-account checks returned current GOOGL/VST REST quotes, verified US
market status and fresh WebSocket trades. The split endpoint returned HTTP 403;
the opening-gap baseline guard is therefore a conservative safeguard, not a
claim of comprehensive corporate-action coverage. Source clock skew of up to
two seconds is tolerated; the default maximum source age is 120 seconds.

The final physical Pi deployment passed readiness, market API checks, restart
persistence, Telegram delivery and zero published ports. Its application-source
fingerprint matches the workspace. Schema 3 preserved the existing watchlist and
preferences. The owner selected Finnhub and enabled monitoring through Telegram;
both settings survived the final deployment restart.

The running Pi stream subscribed to GOOGL, NVDA and VST with validated baselines
and no symbol issues. Source-to-receipt lag in the persisted samples was below
one second. The final inspection found 15 minute observations, a 163,840-byte
database and healthy maintenance. Daily summaries are initially empty until the
actual close plus ten minutes. New controls were also exercised in the isolated
runtime fixtures above; remaining interactive exploration is in the
[Telegram test guide](try-0.3.md). No additional broad Pi test suite was run.

## trabe rename: 0.2.2 (2026-11-03)

The renamed Python package and CLI passed all 88 tests natively and all 88 tests
inside the production container. Both real-HTTP integration tests passed in that
container. A wheel was built, installed into an isolated directory and its
`trabe --help` command executed successfully. Ruff lint/formatting, shell syntax
and the Podman 4.3 Quadlet parser check passed.

The actual remote launcher guard was exercised with the current launcher, the
deployed 0.2.1 launcher and two modified-file controls. Both known versions were
accepted without modifying the installed file; unexpected source/target changes
were rejected. The launcher body, service/data paths and database schema remain
unchanged. These are historical local checks; the Pi upgraded directly to 0.3.
Later milestones in the [roadmap](next-features.md) remain planning work.

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
suites when changing the code. Local results are separate from GitHub Actions runs.

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

## Historical baseline acceptance

The user confirmed `/status`, `/menu`, watchlist additions and watchlist display on
the physical Pi. The 0.2.1 rollout passed its automated gates. User confirmation of
the repeated GOOGL/VST scan remains pending, as does physical reboot verification
during an appropriate maintenance window. These older open checks do not imply
that the physical host was rebooted for the 0.3 deployment; its service restart
and state persistence were verified.

Local logs, private state snapshots, downloaded tools and image archives stay in
ignored local storage. They are not needed to clone the source or run the tests.
