# StockPulse Sentinel

A Python stock watcher controlled entirely through a private Telegram conversation.
It runs on a Debian/Raspbian Raspberry Pi with Podman Quadlet and SQLite. It needs
outbound HTTPS and your existing SSH connection; **no website or new inbound port**.

Version 0.2.1 includes a button menu, persistent watchlist/preferences, cached prices,
background daily-change scans, opt-in weekday summaries, durable reply delivery,
and a tested ARMv7 container. Earnings, insider feeds and AI briefings come later.
The service never places trades.

## Quick start

Requires Python 3.11+ locally. The application has no third-party Python dependencies.

1. Create a dedicated bot with [@BotFather](https://t.me/BotFather), using `/newbot`.
2. Get an [Alpha Vantage key](https://www.alphavantage.co/support/).
3. Run `python3 -m stock_agent setup`. Enter the credentials privately and send its
   one-time code to your bot. Setup binds your private account and saves a Git-ignored
   `.env` with mode 0600.
4. Copy `.env.deploy.example` to `.env.deploy` and set your SSH user, host and port.
5. Run `bash scripts/deploy.sh` from a terminal with SSH access.

Both `.env` and `.env.deploy` stay local and are excluded from Git. See the
[deployment guide](docs/deployment.md) for prerequisites and maintenance commands.

The deployment script authenticates over SSH once, checks the host, builds on its
CPU architecture and verifies Telegram delivery, the market API, restart persistence
and zero published ports. On the known ARMv7 Bookworm Pi, it can install an isolated
Quadlet generator while retaining the existing Podman 4.3 package. That path needs
one small root-owned launcher installed with `sudo`; no OS upgrade is performed.

## Control everything from Telegram

Send `/menu` for persistent buttons. Only the paired user in their private chat can
read or change the app's configuration.

| Command | Effect |
| --- | --- |
| `/menu`, `/help` | Show controls and help |
| `/status` | Service health, watchlist size, scan state and request budget |
| `/test` | Confirm message delivery |
| `/watchlist` | List symbols and tiers |
| `/add IBM 1` | Add/update a symbol; optional tier 1–3, default 2 |
| `/remove IBM` | Remove a symbol |
| `/quote IBM` | Fetch a quote or reuse its cache |
| `/prices` | Show cached watchlist prices, using no API requests |
| `/scan`, `/scan IBM` | Run a background daily-change check and send a summary |
| `/lastscan` | Recover the latest scan result |
| `/settings` | Show preferences and their meanings |
| `/set NAME VALUE` | Change a preference immediately |
| `/schedule 23:00`, `/schedule off` | Enable/disable the weekday summary |

There are no default stocks. An empty watchlist stays empty after restart. `/add`
checks symbol syntax; `/quote` checks the provider's data availability. Tier is a
saved organizational label; it does not change alert severity in this release.

| Preference | Default | Allowed values |
| --- | --- | --- |
| `timezone` | `Europe/Ljubljana` | IANA timezone, e.g. `America/New_York` |
| `quote_cache_minutes` | `60` | 1–1440 |
| `api_daily_budget` | `20` | 1–25 |
| `dip_threshold_percent` | `-4` | -50 to -0.1 |
| `daily_scan_time` | `off` | `HH:MM` or `off` |

Example: `/set dip_threshold_percent -5`. Settings and the watchlist persist in
SQLite and apply immediately. Credentials are bootstrap configuration, entered
locally; do not send API keys to the bot.

## Data freshness, costs and scheduling

Alpha Vantage's default GLOBAL_QUOTE data updates at the end of each trading day.
**These are not live prices.** Every quote and scan shows the actual trading date
and whether data was cached. Prices use the listing currency.

The standard free provider allowance is currently 25 requests/day. This app defaults
to 20, counts attempts including failures, and reuses quotes for 60 minutes. The
counter survives restarts and resets at UTC midnight. Calls by other applications
using the same key are outside this local counter. See [provider limits](https://www.alphavantage.co/support/)
and [quote documentation](https://www.alphavantage.co/documentation/#latestprice).

Version 0.2.1 also spaces uncached provider requests at least 1.1 seconds after the
previous response, sharing the limit between the bot, scan worker and diagnostic
processes. This prevents the free key's one-request-per-second burst limit from
making the second stock in a scan appear unavailable. Provider limit notices are
reported explicitly and trigger a cooldown; cached quotes remain accessible.

Scheduling is **off by default**. `/schedule 23:00` enables at most one scan per
local weekday/date, using the configured timezone. A late restart can run that
day's missed check after the scheduled time. It skips weekends, handles timezone
and DST conversion, and does not include an exchange-holiday calendar. A failed or
interrupted scheduled check is not automatically repeated that day; use `/scan`.

Scans flag daily changes at or below the configured dip threshold. They reuse
cached data, prioritize uncached/older symbols, stop at the request budget or a
provider outage, and report partial results clearly. There is one scan worker;
`/status` remains available while it works. Intraday dip alerts, weekly comparisons,
earnings, insider analysis and AI summaries are not implemented yet.

## Development

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[dev]'
python3 -m unittest discover -s tests -v
python3 -m unittest discover -s tests -p 'integration_*.py' -v
ruff check stock_agent scripts tests
ruff format --check stock_agent scripts tests
bash scripts/check-container.sh
python3 scripts/check_quadlet.py
```

The container check uses no real credentials or internet from inside the test
containers. Its loopback HTTP fixture exercises actual HTTP requests, commands,
background scans, SQLite state and a process restart. ARM validation and recorded
results are described in [validation notes](docs/validation.md).

Live maintenance tools (use only one poller per bot):

```sh
python3 -m stock_agent run
python3 -m stock_agent doctor --market --send
python3 -m stock_agent backup --output /private/path/snapshot.sqlite3
```

`doctor --market` makes one fresh IBM request even if cached; `--send` delivers the
check to your private chat. Online backups are consistent and mode 0600 and refuse
to overwrite an existing destination.

```text
stock_agent/       bot, API clients, preferences, SQLite, reply queue and scans
tests/            unit, deployment-rollback and real-HTTP runtime checks
deploy/           preflight, installer, pinned tool metadata and Quadlet
scripts/          deployment, inventory, tool preparation and verification
docs/             deployment, validation, roadmap and prototype assessment
Containerfile     pinned multi-architecture image; no apt/pip build steps
.env.example      credential names only; no secrets
.env.deploy.example  SSH configuration template; no personal host details
```

Secrets, databases, local tools and validation artifacts are excluded from Git and
the image build context. Deployment does not initialize or push a Git repository.
The [prototype assessment](docs/prototype-review.md) explains the decision to keep Python.

See [CONTRIBUTING.md](CONTRIBUTING.md) for local development and contribution checks.
