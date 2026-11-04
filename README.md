# trabe

A Python stock watcher controlled entirely through a private Telegram conversation.
It runs on a Debian/Raspbian Raspberry Pi with Podman Quadlet and SQLite. It needs
outbound HTTPS/WebSocket connections and your existing SSH connection;
**no website or new inbound port**.

Version 0.7 is ready for deployment: earnings warnings, verified insider-purchase
reports, company-news extracts and an optional morning briefing. The existing
0.4 ledger, EUR/USD portfolio valuation, daily progress and Finnhub dip monitoring
remain available. New automation defaults off. The service records trades you
have already made; it never places orders. See the [0.7 release notes](docs/release-0.7.md).

The [feature roadmap](docs/next-features.md) defines the next releases. The
[storage plan](docs/storage-and-retention.md) keeps minute detail during the session,
rolls it into compact daily summaries and expires history after three months.
The [provider research](docs/market-data-research.md) compares Finnhub, Alpaca,
Tiingo and Twelve Data for future integrations. Alpha Vantage remains an explicit
end-of-day option; outages never silently change the selected source.

## Quick start

Requires Python 3.11+ and the pinned `websocket-client` dependency. Install locally
with `python3 -m pip install -e .`; deployment prepares the verified dependency
automatically without running pip on the Pi.

1. Create a dedicated bot with [@BotFather](https://t.me/BotFather), using `/newbot`.
2. Get a [Finnhub free key](https://finnhub.io/register), and optionally an
   [Alpha Vantage key](https://www.alphavantage.co/support/) for end-of-day quotes.
3. Run `python3 -m trabe setup`. Enter the credentials privately and send its
   one-time code to your bot. Setup binds your private account and saves a Git-ignored
   `.env` with mode 0600.
   Add `FINNHUB_API_KEY=...` privately to that file. A webhook secret is unnecessary
   for the outbound WebSocket feed.
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

See the [0.7 walkthrough](docs/release-0.7.md#one-walkthrough-after-deployment)
for the new features and [0.4 notes](docs/release-0.4.md) for portfolio setup.

| Command | Effect |
| --- | --- |
| `/menu`, `/help` | Show controls and help |
| `/currency EUR`, `/currency USD` | Set default input and reporting currency |
| `/buy GOOGL 5 100 EUR`, `/sell GOOGL 2 125 EUR` | Record executed trades, with optional date and `fee=1.50` |
| `/portfolio`, `/position GOOGL` | Show cached valuations and position results |
| `/trades GOOGL`, `/trade_void ID` | List transactions and void an incorrect entry |
| `/position_adjust GOOGL 10`, `/basis_adjust GOOGL 500 EUR` | Record explicit quantity or remaining-basis corrections |
| `/portfolio_summary 23:00`, `/portfolio_summary off` | Enable/disable daily portfolio progress |
| `/earnings [VST\|on\|off\|refresh]` | Cached schedules, upcoming warnings and date corrections |
| `/insiders [VST\|on\|off\|refresh]` | Qualified purchase reports, source links and alerts |
| `/news [VST\|on\|off\|refresh]` | Source-linked extracts and labelled heuristic sentiment |
| `/briefing [now\|08:00\|on\|off]` | Manual digest or optional scheduled morning briefing |
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
| `/provider`, `/provider finnhub` | Show/select a verified source |
| `/monitor on`, `/monitor off` | Enable/disable intraday collection and alerts |
| `/alerts` | Show effective thresholds and recent events |
| `/alert VST -5`, `/alert VST default`, `/alert VST off` | Set/reset/disable one stock's rule |
| `/pause 24h`, `/resume` | Pause/resume automation; manual commands remain usable |
| `/history VST 5` | Show retained daily observed-price summaries |
| `/storage` | Inspect database, WAL, backups and maintenance |

There are no default stocks. An empty watchlist stays empty after restart. `/add`
checks symbol syntax; `/quote` checks the provider's data availability. Tier is a
saved organizational label; it does not change alert severity in this release.

| Preference | Default | Allowed values |
| --- | --- | --- |
| `default_currency` | `EUR` | `EUR` or `USD` |
| `portfolio_summary_time` | `off` | `HH:MM` or `off` |
| `earnings_enabled`, `insiders_enabled`, `news_enabled`, `briefing_enabled` | `off` | `on` or `off` |
| `briefing_time` | `08:00` | `HH:MM` in the saved timezone; enabling is separate with `/set` |
| `earnings_warning_days` | `5` | 1–14 calendar days |
| `insider_min_value_usd` | `10000` | 1000–1000000000 USD |
| `timezone` | `Europe/Ljubljana` | IANA timezone, e.g. `America/New_York` |
| `quote_cache_minutes` | `60` | 1–1440 |
| `api_daily_budget` | `20` | 1–25 |
| `dip_threshold_percent` | `-4` | -50 to -0.1 |
| `daily_scan_time` | `off` | `HH:MM` or `off` |
| `max_source_age_seconds` | `120` | 30–600; maximum eligible source age |
| `baseline_max_gap_percent` | `25` | 10–50; opening-gap baseline guard |
| `finnhub_daily_budget` | `2000` | 100–10000; independent REST attempt budget |

Example: `/set dip_threshold_percent -5`. Settings and the watchlist persist in
SQLite and apply immediately. Credentials are bootstrap configuration, entered
locally; do not send API keys to the bot.

## Portfolio and FX

Trade syntax is `TICKER QUANTITY PRICE [CURRENCY] [YYYY-MM-DD] [fee=AMOUNT]`.
Currency defaults to `/currency`; the date defaults to today in the configured
timezone. Quantity and price are positive decimals; fees are nonnegative and in
the transaction currency. Corrections retain audit history. A sale or correction
that would make any later sale exceed the available shares is rejected.

Held and watched symbols share the 50-symbol Finnhub subscription allowance.
Removing a watchlist symbol keeps its position and price coverage; it no longer
receives watchlist dip alerts. `/portfolio` and `/position` use cached Finnhub
observations and report missing or stale data; they never request a quote.

The background worker shares daily [Frankfurter/ECB reference rates](https://frankfurter.dev/v1/).
It uses the published rate on or before an execution date, retains direction and
provenance with each trade, and declines rates more than seven calendar days old.
Same-day cross-currency transaction cost is pending until after UTC midnight, so
the completed execution-date reference can be saved. Current FX is checked at
most hourly while needed. Reference rates differ from broker execution rates and
spreads. A currency change changes presentation and new input defaults, never
original trade amounts. Returns include currency movement and recorded fees.

`/portfolio_summary 23:00` opts into daily summaries in the saved timezone;
`/pause` suppresses automatic delivery while manual reports remain available.
Progress compares positions with unchanged trade histories and excludes changed
or unavailable positions, so purchases and sales are not treated as gains.
Transactions and their FX provenance do not expire; compact daily snapshots
expire after three calendar months.

## Data freshness, costs and scheduling

New monitoring defaults off. After deployment, send `/provider finnhub`, then
`/monitor on`. The free stream supports up to 50 supported US-listed USD symbols.
Collection runs only during the verified US core session, including holidays,
early closes and New York DST. Calendar coverage currently ends in 2028; later
dates suspend monitoring until the calendar is updated.

Quotes identify Finnhub's observed trade feed, source UTC time and freshness.
Alerts require a compatible previous-close baseline and a source age of at most
120 seconds by default (up to two seconds of clock skew is tolerated). Stale,
invalid, out-of-order and off-session trades cannot trigger alerts. Free data
coverage can differ from a consolidated broker price.

One alert is claimed per symbol/rule/session, even through restarts, threshold
changes and remove/re-add operations. The free key cannot access the split API;
an opening gap above 25% suppresses alerts pending baseline review. This
conservative guard can also suppress a genuine large opening move.

Minute buckets are rolled up atomically at session close plus ten minutes into
observed first/high/low/last prices, baseline and coverage metadata. Minute detail
is then deleted; daily rows expire after three calendar months. Automatic
pre-deploy backups are limited to three retained plus one protected during a
deployment, with the same age limit. Manual backups remain user-managed.

Pause stops automatic collection and proactive delivery while retaining manual
commands and maintenance. A request/message already in flight may finish. Resume
uses fresh observations without replaying missed alerts. Finnhub REST bootstrap
calls are separately counted, paced and cached; the continuous stream does not
spend Alpha Vantage's budget.

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
`/status` remains available while it works. Earnings, insider purchases and news
use a separate bounded background worker, existing request budgets and cached
views. Briefings can refresh all sections while standalone automation is off.
News summaries are extractive and sentiment is heuristic; model-based summaries,
weekly comparisons and the paper-trading advisor remain future work.

## Development

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[dev]'
python3 -m unittest discover -s tests -q
ruff check trabe scripts tests
ruff format --check trabe scripts tests
```

For an iteration, run the relevant unit file/case and checks on changed files.
Default CI runs basic checks once on Python 3.13. Integration/container checks,
ARM validation and storage soaks are opt-in; see the
[testing policy](CONTRIBUTING.md#testing-policy). Historical verification is
recorded in [validation notes](docs/validation.md).

Live maintenance tools (use only one poller per bot):

```sh
python3 -m trabe run
python3 -m trabe doctor --market --validate-finnhub --send
python3 -m trabe backup --output /private/path/snapshot.sqlite3
```

`doctor --market` makes one fresh IBM request using the selected provider;
`--validate-finnhub` checks GOOGL/VST and market status when a key is configured.
`--send` delivers the check to your private chat. Online backups are mode 0600 and refuse
to overwrite an existing destination.

```text
trabe/            bot, API clients, preferences, SQLite, reply queue and scans
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
