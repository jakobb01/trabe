# Project guide

trabe is an owner-only Telegram service running on a Raspberry Pi with Python,
SQLite and rootless Podman Quadlet. Configuration lives in Telegram and SQLite;
bootstrap credentials live in private environment files. It records executed
trades and produces research messages, never brokerage orders.

This is the maintained public reference. Development/testing policy is in
[CONTRIBUTING.md](../CONTRIBUTING.md). Private handoff, research and historical
release evidence are kept outside Git.

## Telegram controls

Use Telegram's expandable command menu or `/help`. Startup, `/start`, `/menu`
and `/help` remove the old large reply keyboard. Only the paired owner in their
private chat can read or modify the app.

| Commands | Behavior |
| --- | --- |
| `/status`, `/test` | Service health and delivery check |
| `/watchlist`, `/add TICKER [1\|2\|3]`, `/remove TICKER` | Saved watchlist; tier defaults to 2 |
| `/quote TICKER`, `/prices` | Request a quote or inspect cached watchlist prices |
| `/scan [TICKER]`, `/lastscan` | Background daily-change check and last result |
| `/provider [finnhub\|alphavantage]` | Inspect/select the market provider |
| `/monitor on\|off`, `/alerts` | Intraday monitoring and alert status |
| `/alert TICKER -5\|default\|off` | Per-symbol dip threshold |
| `/history TICKER 5`, `/storage` | Retained daily observations and storage health |
| `/settings`, `/set NAME VALUE` | Saved preferences |
| `/set timezone Europe/Ljubljana` | IANA timezone for display and local scheduling |
| `/schedule HH:MM\|off` | Weekday daily-change scan schedule |
| `/pause 24h`, `/resume` | Pause/resume automation; manual commands remain available |

There are no default symbols. Removing a watched symbol does not delete its
holdings. Held and watched symbols share the 50-symbol subscription allowance.
Tier is an organizational label, not alert severity. Telegram does not report
timezone changes when you travel; change the saved timezone explicitly.

### Portfolio and FX

Trade syntax is `/buy TICKER QUANTITY PRICE [EUR|USD] [YYYY-MM-DD] [fee=AMOUNT]`;
`/sell` uses the same format. Currency defaults to `/currency EUR|USD`; date
defaults to today in the configured timezone. Money uses decimal arithmetic and
weighted-average cost. Fees use the trade currency.

`/portfolio` and `/position TICKER` use cached valuations. `/trades TICKER` lists
transactions; `/trade_void ID` retains an audit trail while correcting an entry.
`/position_adjust TICKER QUANTITY [DATE]` adjusts quantity while preserving basis;
`/basis_adjust TICKER AMOUNT EUR|USD [DATE]` corrects remaining cost basis. Changes
that would make a later sale oversell the position are rejected.

Frankfurter/ECB reference FX is saved with transactions using the published rate
on or before the execution date; rates over seven calendar days old are rejected.
Same-day historical cross-currency costs remain pending until after UTC midnight.
Current conversion rates are checked at most hourly while needed. Reference rates
can differ from broker fills and spreads. Changing display currency never rewrites
original transaction amounts.

`/portfolio_summary HH:MM|off` controls daily progress. Progress compares positions
with unchanged trade histories so purchases and sales do not appear as gains.
Missing/stale prices, FX and partial totals remain visible.

### Company reports and briefing

`/news [TICKER]`, `/earnings [TICKER]` and `/insiders [TICKER]` queue a background
refresh and deliver the report. Add `cached [TICKER]` for a no-network view;
`refresh [TICKER]` explicitly refreshes. Each supports `on|off` for standalone
automation. `/insider` aliases `/insiders`; `/news sources` shows source status.

| Briefing command | Behavior |
| --- | --- |
| `/briefing now` | Refresh all sections in the background and deliver the overview |
| `/briefing cached` | Immediately render the compact overview from current caches |
| `/briefing details` | Render the fuller cached digest, including individual stocks |
| `/briefing HH:MM`, `/briefing on\|off` | Configure the optional morning briefing |

The overview includes local market times, SPY/QQQ and VIX observations, saved
outlook or issue time, portfolio totals, up to three market extracts and one
earnings/insider card each. Source failures, stale prices and partial totals stay
visible. Whole sections that exceed one message are deferred to details rather
than cut midway through a qualification. Cached views render current data, not
an immutable historical briefing.

Briefings may refresh sections whose standalone automation is off. The scheduled
time uses the local date of a verified US session open and must precede that open;
session-based claims prevent duplicate delivery across travel/restarts.

### Market outlook

`/outlook now` (or `/outlook`) refreshes SPY/QQQ, VIX and broad-market news, then
saves and delivers an expectation. `/outlook cached` or `/outlook YYYY-MM-DD`
reads saved text. `/outlook on|off` controls daily issuance, preparation and later
assessment. `/outlook results` reports assessed scheduled outlooks, excluding
manual experiments from the daily score.

Automatic issuance is 30 minutes before the verified US open, separately from
the earlier briefing. Preparation starts two minutes earlier. A five-minute
issuance window is allowed; missed windows are recorded without backfill.
Manual requests are pre-open and limited to one issuance per minute.

Both SPY and QQQ at least +0.20% versus verified prior closes give a bullish opening
expectation; both at or below -0.20% give bearish. Other valid flat/divergent inputs
are mixed. Missing, future, wrong-session or over-20-minute-old premarket inputs
give insufficient evidence. Session bias additionally requires recent market
news and usable VIX context. VIX indicates volatility context, not direction.

Issued text, exact consumed facts, timestamps, rule versions and parameters are
frozen before outcomes. Later assessments are separate immutable records. Opening
gap uses the first regular-minute open against the frozen previous close; session
return uses the last regular-minute close against that opening price. These are
indicative price proxies, not official auctions or total returns. Both horizons
use the predeclared 0.20% joint benchmark categories. Missing endpoints and
abstentions are unscored; results include simple baseline comparisons. Endpoint
collection begins at least 30 minutes after close; missing outcomes become
unscorable after seven days.

### Five-session company advisor

`/advisor TICKER` saves research for a held/watched company from cached evidence.
It does not request data. Refresh company reports before first issuance when
needed. `/advisor cached TICKER` reads the last record; `/advisor evidence TICKER`
includes sources, the reference price, input hash and assessment contract.

The horizon is the next five **complete** US sessions, starting at an open strictly
after issuance and ending at the fifth close. One immutable suggestion is allowed
per ticker/first target session, including an abstention. Repeated commands return
that record; later data cannot revise it.

The initial rules inspect up to five newest direct company story groups and
require two distinct stories with extracts published within 72 hours, successful
news collection within three hours, no retained news-source errors, and a usable
USD Finnhub reference price. Future observations are excluded. Each eligible
story's local bias is bullish at >= +0.2, bearish at <= -0.2, otherwise mixed.
Unanimous categories without conflicts give a directional expectation; conflicting
stories give mixed. Missing required evidence produces an abstention. Vendor
scores are not averaged into this direction; no historical momentum is consumed.

Suggestions are research actions: investigate adding, review reducing a held
position, avoid adding, hold/watch or wait for evidence. Known earnings within
the horizon defer bullish additions to waiting for earnings. Missing fundamental
coverage, insider context and the saved first-session market outlook remain risk
context. Confidence is uncalibrated; there are no allocations or trade orders.

**Company outcome scoring and paper trading are not implemented yet.** The frozen
assessment contract specifies first regular-minute open to fifth-session last
regular-minute close, a ±1% flat band and same-window SPY comparison. Missing
endpoints and abstentions are unscored. Future collection must preserve opening
observations before minute history expires. Fees, dividends and simulated fills
are not part of this price-return contract.

## Data sources and limits

The default design uses free access, persistent caches, shared request budgets
and cooldowns. An HTTP success does not establish financial correctness.

- Finnhub supplies observed US-stock prices, company news and earnings data.
  Monitoring follows verified core sessions, holidays, DST and early closes.
  The bundled calendar covers through 2028; expired coverage stops session-based
  activity until updated. Alerts require fresh prices and an accepted previous
  close. A default 25% baseline-gap guard suppresses potentially incompatible
  prices; it can also suppress a genuine large move. Alert claims survive restarts.
- Yahoo supplies indicative premarket minute observations, SPY/QQQ, the actual VIX
  index, RSS and supported public article extracts. Equity feed delay is unverified;
  VIX is labelled 15-minute delayed or prior-session context. VIX's closing window
  extends 15 minutes beyond the equity close. Prior closes are never silently
  presented as current premarket prices. Yahoo charts share the app's 120/hour
  allowance, with a 60/hour chart sublimit.
- Earnings reports preserve actual/estimate source, fiscal period, currency and
  EPS basis. Strict public-news evidence can fill a structured-feed gap without
  claiming a beat/miss against an unverified estimate. Calendar forecasts remain
  separately labelled. Alpha Vantage earnings-result ingestion remains disabled
  after a financial-data mismatch; parser tests are not value validation.
- Insider reports combine Tracefour and Finnhub. Alerts require meaningful
  qualified non-derivative purchases and eligible roles. Partial source ranges
  and unknown eligibility stay explicit; a healthy empty feed is not an outage.
- Company news is grouped to reduce syndicated duplicates and carries saved
  provenance. Local signed bias is a heuristic, not calibrated probability.
  Optional Massive/Marketaux sources require explicit settings and appropriate
  keys/access. A configured key does not automatically enable an extra source.
- Alpha Vantage is an optional end-of-day quote provider. The app defaults to
  20 attempts/day and a 60-minute quote cache, with persistent pacing/cooldowns.
  End-of-day prices are not live quotes and outages do not silently change provider.

Useful settings include `timezone`, `default_currency`, `briefing_time`,
`news_sources`, `news_second_view`, `earnings_warning_days` (default 5),
`insider_min_value_usd` (default 10000), `dip_threshold_percent` (default -4) and
`max_source_age_seconds` (default 120). `/settings` shows the current values and
meanings. New automations default off; `/pause` suppresses proactive work and
delivery while preserving manual commands and maintenance. A request already
in flight may finish. Resume does not replay missed dip alerts.

The older `/schedule` daily-change scan is weekday-based, not exchange-calendar
aware. Briefings, market outlooks and monitoring use the verified exchange calendar.

## Persistence and retention

Schema 9 adds company research to the existing ledger, news, decision snapshots
and market outlooks. Existing immutable packets are not rewritten during upgrades.
Financial transactions and their FX provenance do not expire with market history.

| Data | Retention / bound |
| --- | --- |
| Real trades and saved transaction FX | Durable audit history |
| Intraday observations | Minute buckets; roll up at session close +10 minutes, then delete minute detail |
| Daily price summaries and portfolio progress | Three calendar months |
| Decision snapshots | Scheduled: three months; manual: seven days / 32 records; 256 KiB per packet |
| Market outlooks | Scheduled: three months; manual: seven days / 32 records; 64 KiB per packet; assessments expire with parent |
| Company advice | Three calendar months, 256 records, 64 KiB each; count limit rejects new records rather than evicting unfinished forecasts |
| News and signal history | Bounded per-symbol/global caches with three-month historical expiry |
| Parsed public articles | 32 documents, 128 KiB each; six-hour refresh, seven-day expiry, one-hour negative cache |
| Automatic database backups | Three retained plus one protected during deployment; three-month age limit |
| Manual database backups | User-managed; never pruned automatically |
| Queued messages / completed refresh jobs | Expire after 24 hours |

`/snapshot now` creates an immutable input packet, not a database backup.
`/snapshot latest|YYYY-MM-DD|on|off` inspects or controls capture. Automatic capture
defaults off, normally 30 minutes before the US open, with no backfill beyond five
minutes. Snapshot v1 stores selected prices/baselines rather than a full price
history window. Hashes, source identities and knowledge timestamps preserve auditability.

Maintenance continues while monitoring is off. Low disk reserve (below 100 MiB),
excessive WAL growth or an oversized database can pause collection to preserve
recovery space. `/storage` reports the reason; avoid deleting runtime data to free
space without inspecting the cause.

## Deployment

The deployment machine needs Python 3.11+, Bash, SSH and tar; WSL is supported.
The Pi needs a regular user with rootless Podman, cgroup v2, working systemd user
services and lingering for boot startup. Host preparation is separate from the
app's runtime. An administrator can install missing prerequisites:

```sh
sudo apt-get install podman uidmap slirp4netns fuse-overlayfs dbus-user-session
sudo loginctl enable-linger containers
```

Replace `containers` with the service account. Native Quadlet is preferred. The
ARMv7/Podman 4.3 compatibility path stages a pinned, checksum-verified ARMhf
generator and installs one app-scoped root-owned launcher with `sudo`; it does
not replace Podman or upgrade the OS. The launcher is gated to trabe's private
definitions. After initial bootstrap, application updates are rootless.

Create/pair credentials with `python -m trabe setup`, then add `FINNHUB_API_KEY`
privately to `.env`. Optional key names are in [.env.example](../.env.example).
Copy [.env.deploy.example](../.env.deploy.example) to `.env.deploy` and configure
`SSH_TARGET`, `SSH_PORT`, and optionally `SSH_IDENTITY_FILE`/`SSH_CONFIG_FILE`.
These files are read as text, never sourced as shell code. Environment variables
override file settings; `DEPLOY_ENV_FILE` selects another SSH configuration file.

```sh
mkdir -p .local
set -o pipefail
bash scripts/deploy.sh 2>&1 | tee .local/deploy.log
```

Verify SSH host fingerprints normally; host-key conflicts are not bypassed.
Only one process may poll a bot token. Preflight rejects a competing legacy
system-wide service rather than stopping unrelated services.

The installer builds the Pi's architecture, backs up a running prior database,
retains prior image/configuration, requires a successful Telegram poll, checks
market access, restarts and compares saved settings/watchlist/trades, verifies
zero published ports, and sends a Telegram confirmation. Wait for `Deployment
verified` before editing those compared records. Then do one walkthrough relevant
to the changed feature; do not repeat all local tests on the Pi.

On a failed rollout, the installer restores prior configuration/image and, when
available, its pre-deploy database snapshot. Writes during a failed rollout can
be lost on restoration. Verify service state after a failure: rollback does not
guarantee a usable prior installation exists. Keep the matched backup for schema
rollback; do not run an older app against a newer database schema.

### Operations and backups

Existing installations retain the `stock-agent` service/container names:

| Location | Purpose |
| --- | --- |
| `~/.config/stock-agent/agent.env` | Private credentials, mode 0600 |
| `~/.config/containers/systemd/stock-agent.container` | Native Quadlet definition |
| `~/.config/stock-agent/quadlet/stock-agent.container` | Compatibility definition |
| `~/.local/libexec/stock-agent/quadlet` | App-scoped compatibility generator |
| `stock-agent-data` Podman volume | Database, reply queue and backups |
| `localhost/stock-agent:current` / `:previous` | Active and rollback images |

Run these maintenance commands on the Pi as the service user:

```sh
systemctl --user status stock-agent.service
journalctl --user -u stock-agent.service -n 80 --no-pager
podman exec stock-agent python -m trabe health --ready
podman port stock-agent
```

`podman port` should print nothing. The container has a read-only root filesystem,
dropped capabilities and a writable named data volume. Quadlet supplies boot
dependencies; do not manually enable its generated transient service.

For a consistent private backup, choose a fresh destination filename:

```sh
podman exec stock-agent python -m trabe backup --output /data/manual-backup.sqlite3
podman cp stock-agent:/data/manual-backup.sqlite3 ./manual-backup.sqlite3
chmod 600 ./manual-backup.sqlite3
```

Back up credentials separately. Never remove the named volume during an update.
`bash scripts/inspect-server.sh` provides a read-only host inventory from the
deployment machine. `python -m trabe doctor --market --validate-finnhub --send`
performs real provider requests and sends a Telegram message; it is not an offline
test. Replies persist before delivery and acknowledge chunks individually, but a
lost Telegram HTTP response can still cause duplicate delivery.

## Roadmap

1. Collect and score company outcomes separately from saved five-session research;
   capture the opening endpoint while minute history is still available.
2. Define paper execution, starting cash, allocation limits, fees/slippage,
   benchmarks and experiment retention, then implement a separate paper ledger.
3. Evaluate prospective forecast usefulness and paper performance independently
   before expanding model complexity or research claims.

Richer publisher coverage, weekly comparisons, alert recovery rules and portfolio
risk views remain possible follow-ups. Automated brokerage execution is outside
the current scope.
