# Contributing

Use Python 3.11 or newer. Runtime and development dependencies are pinned in
`pyproject.toml`. The one runtime dependency is the pure-Python WebSocket client.

## Testing policy

Keep the development loop small. For an ordinary change, run the relevant unit
test file or case and lint/format checks on changed Python files. Add a small
regression test when it protects actual behavior, especially money arithmetic,
owner authorization, saved state or alert deduplication. Keep existing useful
tests; do not add tests that just mirror the implementation.

Documentation-only changes need a diff/link review, not app tests or a deployment.
Check shell/CI syntax when those files change. Once the relevant checks pass,
reuse that result until another change or failure gives a reason to rerun it.

Default GitHub Actions runs lint, formatting, shell syntax and the unit suite
once on Python 3.13. There is no default Python matrix, integration run, container
build, ARM emulation or storage soak.

Extended checks are opt-in. Use one when explicitly requested or when a concrete
change/failure needs it: a transport change can need the HTTP/WebSocket fixture;
a dependency/image change can need a container check; a retention change can need
a storage soak. State the reason, choose the relevant check and run it once.
Do not run the entire menu for a routine feature. A deployment uses its existing
readiness/delivery checks plus one relevant Telegram walkthrough, avoiding a
second copy of the same local validation on the Pi.

## Basic local checks

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[dev]'
ruff check trabe scripts tests
ruff format --check trabe scripts tests
python -m unittest discover -s tests -q
for script in scripts/*.sh deploy/*.sh; do bash -n "$script"; done
sh -n deploy/stock-agent-quadlet-generator
```

For a focused intraday change, for example, use
`python -m unittest discover -s tests -p test_intraday.py -q` instead of rerunning
the full unit suite. Tests use fixture credentials and temporary databases.

## Optional extended checks

Run only what the change needs:

```sh
python -m unittest discover -s tests -p 'integration_*.py' -v
# Build once and check the restricted runtime; no unit/integration/soak replay.
bash scripts/check-container.sh
# Include the HTTP/WebSocket fixture when needed.
CHECK_EXTENDED=1 bash scripts/check-container.sh
# Include the storage soak only when needed.
CHECK_SOAK=1 bash scripts/check-container.sh
python3 scripts/check_quadlet.py
```

The integration fixture uses loopback HTTP/WebSocket connections. The container
check needs rootless Podman and downloads the pinned image/runtime dependency if
absent; its checks run without external networking or real credentials. The
Quadlet check needs a local generator. ARM/QEMU validation is reserved for an
identified architecture issue or a relevant runtime/dependency change.

In GitHub Actions, choose **Run workflow** and enable `extended` or `soak` only
when needed. `extended` adds Python 3.11 unit compatibility and production-image
HTTP/WebSocket checks. `soak` adds the longer storage run. Existing scripts and
test coverage remain available without making them routine development gates.

## Project boundaries

Keep Telegram as the configuration interface. Store watchlists and preferences in
SQLite; keep bootstrap credentials in the private environment file. Provider calls
must share the persistent cache, request budget and pacing in `MarketData`.

Only one running instance may poll a given Telegram bot. Use the fixture tests for
development while the Pi is running. Running `python -m trabe run` locally
with the same real bot token would compete with the deployed service.

Keep source and deployment changes together when they affect the container or
stored state. Preserve backups and rollback behavior. Explain data migrations and
changes to Telegram commands in the relevant documentation.

## Files kept outside Git

`.env`, `.env.deploy`, runtime databases, logs, local tool downloads, archives and
`.local/` are ignored. The two `.example` files contain placeholders and belong in
Git. Keep personal hosts, chat IDs and credentials out of examples, fixtures and
issue reports.

Before a commit, inspect `git diff --cached` and `git status --short`. The initial
source tree does not include the original prototype archive or private deployment
session notes.
