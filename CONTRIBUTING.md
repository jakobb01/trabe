# Contributing

Use Python 3.11 or newer. Runtime and development dependencies are pinned in
`pyproject.toml`. The one runtime dependency is the pure-Python WebSocket client.

## Local checks

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[dev]'
ruff check trabe scripts tests
ruff format --check trabe scripts tests
python -m unittest discover -s tests -v
python -m unittest discover -s tests -p 'integration_*.py' -v
python -m tests.soak_intraday
for script in scripts/*.sh deploy/*.sh; do bash -n "$script"; done
sh -n deploy/stock-agent-quadlet-generator
```

The unit tests simulate external services. The HTTP/WebSocket integration tests use a
loopback fixture. Neither needs real API keys, Telegram messages or access to a Pi.
GitHub Actions runs these checks on Python 3.11 and 3.13, plus the production
container checks.

With rootless Podman installed:

```sh
bash scripts/check-container.sh
python3 scripts/check_quadlet.py
```

The Quadlet check requires a local Quadlet generator. The container build downloads
the pinned Python base image when it is not already available. Container tests
then run without external networking or real credentials.

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
