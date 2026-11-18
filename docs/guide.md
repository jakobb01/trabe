# Project guide

trabe is an owner-only Telegram service running on a Raspberry Pi with Python,
SQLite and rootless Podman Quadlet. Configuration lives in Telegram and SQLite;
bootstrap credentials live in private environment files. It records executed
trades and produces research messages, never brokerage orders.

This is the maintained public reference. Development/testing policy is in
[CONTRIBUTING.md](../CONTRIBUTING.md). Private handoff, research and historical
release evidence are kept outside Git.

## Telegram controls

Open `/home` for Today / Companies / Portfolio / Settings. Company cards link to
News / saved Research / Earnings / Insiders. News has Sources / More / Refresh;
Refresh queues the existing background job and returns cached status immediately.
Cached navigation never requests provider data, issues research or starts paper
trading. Research creation remains the explicit `/advisor TICKER` command.

The expandable everyday menu has home, briefing, news, portfolio, status and help.
`/help advanced` lists every existing advanced command. `/start` and `/menu` open
Home and remove the old large reply keyboard; startup and `/help` also remove it.
Only the paired owner in their private chat can read or modify the app. Buttons
expire after 24 hours; reopen `/home` if a card has expired. Settings buttons
explicitly set on/off and cannot repeat a mutation after callback replay.

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
outlook or issue time, portfolio totals, up to three material company developments, three market extracts and one
earnings/insider card each. Company events rank across all held/watched companies,
with multi-company copies deduplicated. Source failures, stale prices and partial totals stay
visible. Whole sections that exceed one message are deferred to details rather
than cut midway through a qualification. Cached views render current data, not
an immutable historical briefing.

Briefings may refresh sections whose standalone automation is off. The scheduled
time uses the local date of a verified US session open and must precede that open;
session-based claims prevent duplicate delivery across travel/restarts.

### Company developments and news alerts

`/news cached [TICKER]` shows local event cards with named sources, materiality,
price uncertainty and whole excerpts. The card leads with the company-specific
claim when the retained source supplies one. Compact cards can include a second
excerpt when it supplies a material qualifier. Event rules inspect bounded source clauses,
so a speculative headline does not erase a supported body disclosure. Explicit CEO
transitions, reported financial results/asset impairments and binding regulatory
actions or settlements outrank routine previews. Approvals/compliance are medium
materiality by default. A major event can have
unknown price impact. Unresolved actors and speculative claims stay uncertain;
company statements are attributed, not independently verified. Relationships are
small, curated and dated; coverage is not a general understanding of company news.
Cards distinguish company developments, related reporting and commentary.
Cards use one company heading and credit the source of the displayed excerpts.
Cached views can select clearer excerpts from older retained evidence at read
time. Frozen event revisions and alert eligibility keep their original meaning.
Newly enqueued alerts use the same compact excerpt selector.
The morning briefing can fill one remaining company slot when a source sentence
names the issuer, reports results and says the company raised or cut forward
guidance. The card explains why it appears. Alert eligibility still follows the
saved event classification.

#### Review samples from scheduled briefings

When the scheduled morning briefing queues, it records up to three company news
cards actually included in the message. Each sample keeps the credited source
packet, the previous compact selector's first frozen excerpt, the new excerpts
and exact card text, with timestamps, ticker, and selector versions. The pair
compares excerpts for the same event. The stored briefing records the queued
message. Only company news cards shown in scheduled briefings are sampled.
Sampling makes no extra requests or messages.

Review a local database copy. Samples expire after 14 days or when the newest
30 fill the limit, with up to 16 KiB of JSON per card. Normal news maintenance
prunes expired rows. This query includes queued briefings with zero displayed
cards. A null displayed count means the run did not record that count. A lower
sample count can reflect unavailable evidence, a size limit, or retention.

```sql
SELECT n.event_key, datetime(n.created_at, 'unixepoch') AS queued_utc,
       json_extract(b.payload, '$.news_cards_displayed') AS displayed,
       COUNT(s.position) AS retained_samples
FROM signal_notifications AS n
LEFT JOIN signals AS b ON b.kind = n.kind AND b.event_key = n.event_key
LEFT JOIN news_summary_samples AS s ON s.briefing_key = n.event_key
WHERE n.kind = 'briefing'
  AND n.created_at >= CAST(strftime('%s', 'now', '-14 days') AS INTEGER)
GROUP BY n.event_key, n.created_at, displayed
ORDER BY n.created_at DESC;

SELECT briefing_key, position, ticker, datetime(as_of, 'unixepoch') AS as_of_utc,
       payload
FROM news_summary_samples
ORDER BY as_of DESC, position;
```

News cards show age in hours (for example, “5h ago”), without an absolute date or clock time. Active cards cover 72 hours. Still relevant major events from the last seven days
appear separately with their age in hours. `/news sources` shows specific per-source
coverage; a complete response means only that provider's fixed interval was read.
Finnhub/Yahoo caps, source failures, unsupported issuers, stale caches and retention
gaps remain visible. At storage caps, history can be shorter than the 14-day target.
Pending notification evidence is protected; overflow is deferred or coverage marked
partial. There is no promise of complete weekly or real-world news recall.

NVIDIA, AMD and Vistra official RSS feeds supplement the existing providers hourly.
Official extracts are short and attributed. Telegram views omit URLs throughout,
including legacy replies and detailed reports; original URLs stay in the internal
evidence audit. The recurring “Coverage is partial” footer is omitted. Amazon and Alphabet official IR
requests returned 403 at onboarding; their official feeds remain unsupported.
No general crawler or access workaround is used. Issuer fetches are bounded to
512 KiB, conditional where supported, with fixed HTTPS URLs and no redirects.
At most two reviewed public issuer pages are considered for missing text per
symbol refresh, independently of Yahoo. No additional full-text cache is created.

Massive continuation uses the same 100-item page size, at most two extra pages per
symbol pass and 40 extra requests/day within the saved budget. At least 24 requests
per configured held/watched symbol/day are reserved for hourly baseline collection
(120 for five symbols); a budget smaller than this leaves no extra pages. Each
fixed scan resumes after committed evidence, with six-hour watermark overlap.
Fresh first pages continue separately while a backlog catches up. Unsafe next URLs,
repeating/expired cursors and budget deferral produce visible partial intervals.
The presentation worker runs separately and yields during pre-open preparation.

New sources, extra pages and new event rules enrich presentation only. The advisor,
paper experiment and frozen historical packets retain their existing legacy news
baseline. `/news details [TICKER]` exposes that older sentiment analysis; it is not
the richer presentation digest. No strategy-rule version changes with this release.

Material news pushes are **off by default**, including on upgrades. Enable them
explicitly with `/set news_alerts_enabled on` or Settings; `/news on` is also
required. Defaults: `news_alert_daily_cap=3` (0–10), quiet hours 22:00–08:00 with
`news_quiet_enabled=on` in the saved IANA timezone. Set `news_quiet_start` and
`news_quiet_end` to change the interval. Equal start/end means all-day quiet.
There is no urgent bypass. Quiet candidates coalesce; stale ones expire rather
than building an unlimited queue. Caps count enqueued messages conservatively
per local day and rolling 24 hours, including messages later dropped.

Only fresh, directly supported, reported current high-materiality events with
displayable supporting facts, or substantive corrections
to previously notified events qualify. Uncertain clustering, competition claims,
indirect/incidental context, recaps, previews, opinion and source outages remain
digest-only. Initial alerts require the event's direct issuer to be held/watched;
another issuer's incidental audience cannot authorize a push. Saved alert preferences
are preserved on upgrade. Enable/resume records a
new observation watermark and generation; old stories are seeded silently. There
is at most one initial enqueue and one substantive follow-up per event. Disable,
pause, storage pressure, generation changes, removed companies, quiet time and
expiry are rechecked before delivery. A news hold does not block other replies.
Semantic enqueue identity survives restarts; Telegram response loss can still
cause a duplicate delivery. Exactly-once Telegram delivery is not promised.

The fixed labeled corpus is primarily synthetic; archived feed examples are
separately labeled. Fixture attribution/ranking scores do not establish real-world
coverage or predictive skill. Prospective quality evaluation and owner comparison
remain separate from parser and transport checks. Paper results remain on demand,
with no automatic paper/outcome summaries or hosted writer.

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
needed. Companies → Research and `/advisor cached TICKER` show the last saved
forecast and label its window as upcoming, underway or complete. When no record
exists, they show a read-only preview from the current cache. Use
`/advisor preview TICKER` to inspect current evidence and gaps before saving.
`/advisor evidence TICKER` includes saved sources, the reference price, input
hash and assessment contract.

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

`/advisor track on|off` controls silent background outcome collection (default
off). `/advisor results [TICKER]` reports match counts, sample/coverage bounds,
pending, abstained, unscorable and unsupported records separately. Always-bullish
and always-mixed baselines use the same eligible cohort; SPY comparisons state
their smaller paired denominator. `/advisor result ID` inspects one result or
pending state; `/advisor record ID` reads that exact original forecast. These
views use saved data and never issue new advice or request prices.

The frozen assessment contract uses first regular-minute open to fifth-session
last regular-minute close, an inclusive ±1% directional threshold and same-window
SPY comparison. The collector saves the opening endpoint from open +5 minutes,
and the closing endpoint from close +30 minutes. It makes at most two requests
per pass, sharing a 12/hour collector allowance with paper trading inside Yahoo's
existing budgets. Failed endpoints retry no sooner than hourly, until seven
calendar days after their respective session closes. Removed watchlist symbols
remain collectable while their forecasts are retained.

Observations and assessments are immutable and hash-checked. Missing company
endpoints and abstentions are unscored; missing SPY endpoints still permit a company
direction score with no benchmark/excess return. Known split/identity conflicts
are unscorable. Returns are **unadjusted indicative price returns; corporate-action
coverage is unverified**. Fees, dividends and simulated fills are outside this
contract. Overlapping windows are not independent samples; hit rate is not profit.
Collection respects global pause/storage guards and sends no new daily messages.

`/advisor compare [TICKER]` compares the original rules with two alternatives on
new records. Non-neutral consensus ignores neutral votes and needs all directional
votes to agree. Mean story bias averages all eligible story biases and calls
bullish at +0.10 or above and bearish at −0.10 or below. Both calls use the
original evidence gates and stay mixed if a story group has a conflict. They are
saved with the original packet at issuance. Older packets remain readable, and
previews use only the original research rules.

The comparison shows frozen and scored directional coverage, input gaps, method
disagreements, pending/abstained/unscorable records, and matches on identical
eligible records. On scored disagreements it counts candidate wins, baseline
wins, and cases both missed. It reports owner-issued and automatic cohorts separately.
The disjoint summary selects earliest-ending scored windows first, without
shared target sessions, and keeps all scored companies in each selected window.
Companies within a window share market exposure. These are prospective observations.

`/advisor shadow on|off` controls quiet automatic collection (off by default).
`/advisor shadow` shows the next sampling window and recorded checkpoints. On each
verified trading day, the worker samples up to five held or watched companies on
a rotating alphabetical schedule between 09:20 and 09:29 New York time. The five
target sessions start that day. It uses the existing point-in-time cache and
records the selected companies, evidence gaps or omissions, and frozen calls.
Checkpoints are written once per session. A missed window stays a gap in the
record. Earlier preferences about alerts, manual assessment and paper trading
apply independently.

The automatic cohort uses the same price endpoints and schedule, sharing the
existing request limit. An abstention needs no endpoint requests. Shadow
capture and scoring respect the global pause, storage guard and shadow setting.
They send no Telegram messages. Checkpoints and up to 384 automatic records are
retained for three calendar months. The first scores need five complete
sessions plus endpoint collection. Even a full week of captures has overlapping
windows.

### Daily paper experiment

`/paper start` creates one separate experiment with **USD 2,000** virtual cash and
enables processing. Repeating start returns the same experiment; `/paper on|off`
resumes/pauses it without another deposit. Off or global pause cancels outstanding
instructions, releases reservations and preserves holdings. There is no manual
order, backdate, reset or automatic fifth-session liquidation command. The real
`/buy`, `/sell` and portfolio ledger stay separate.

| Command | Saved view |
| --- | --- |
| `/paper`, `/paper status` | Cash, reservations, holdings, timing and source gaps |
| `/paper decisions [YYYY-MM-DD] [PAGE]` | Eight saved decisions per page, targets and actual order states |
| `/paper decision ID` | Exact consumed inputs, components, timestamps, execution/FX evidence and fees |
| `/paper trades [TICKER]` | Latest twelve simulated fills |
| `/paper results` | Basis, realized net P&L, fees and explicitly dated indicative marks |

Preparation starts one hour before a verified US open, reviewing paper holdings
first, then the watchlist. Decisions are captured once **15 minutes before open**,
with a one-minute capture window and no late backfill. Every watched symbol plus
removed-but-held symbols receives a saved decision. Missing data produces a hold
with reasons. Preparation respects source refresh intervals and shared budgets;
it does not enable standalone alerts. All result views are cached and send no
requests. Both paper processing and company scoring default off independently.

The frozen `paper-rules-v1` classifier combines up to five distinct direct news
events (mean bias ≥0.2 gives +2, ≤−0.2 gives −2), comparable recent earnings
(+1/−1), qualified recent insider purchases (+1) and the same-session market
outlook (+1/−1). News needs two extracts within 72 hours and a successful refresh
within three hours. Fundamental collection health must be within 24 hours;
earnings/insider events use a 30-day window. Earnings comparisons require explicit
proof of matching period, basis and currency for both values. Existing calendar
pairs without that proof stay neutral with a coverage gap. Unavailable optional
coverage caps purchases at small; it does not itself force reductions.

A score ≤−2 targets zero, −1 through +1 holds shares unchanged, and +2/+3/+4 or
more targets small/medium/big holdings worth **$200/$400/$600**. These are total
position targets. Whole shares are sized against a validated previous-session
close. Upcoming earnings today/next session blocks purchases, and removed symbols
can only hold/reduce/sell. Purchases require USD EQUITY identity. Rule weights and
freshness defaults are engineering baselines, with no validated profitability or
numerical confidence claim.

Orders simulate at the exact first regular-minute open, first collected at open
+5 minutes. There is zero slippage and at most one fill per ticker/session across
restarts. Opening gaps can reduce/cancel quantity; a frozen direction cannot flip.
No fractional shares, borrowing or sale of unowned shares is allowed. Each executed
purchase **and** sale costs **EUR 1 once**, converted to cents with the latest valid
Frankfurter/ECB observation actually received by that open, at most seven calendar
days old. Later FX updates never revise a historical fee. No eligible FX cancels
the order; unfilled instructions expire at close +30 minutes.

Buy funding is reserved by descending score, then ticker, including commission.
Same-open sale proceeds become available next session. Cash/basis/P&L replay from
immutable fills, with weighted basis allocated on partial sales. No new daily
messages or brokerage connection is involved. A known corporate action blocks new
orders and valuation until explicit support exists; that block survives source
cache expiry. Corporate-action coverage remains unverified; positions are never
silently adjusted for an inferred split.

Valuations use saved validated closes with dates. Total equity/return is available
only when every holding has a common-session mark; missing stocks are never valued
at zero or presented as freshly marked. Realized P&L remains available separately.
For example, buying four shares at $50 and selling them at $55 with EUR/USD 1.10
ends at $2,017.80 cash after the two $1.10 fees.

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

Schema 12 adds a separate presentation event audit, notification identities, UI
sessions and typed message edits alongside company outcomes, immutable price
endpoints, paper evidence, FX receipts, the real ledger, snapshots and outlooks. Existing immutable packets are not rewritten during upgrades.
Financial transactions and their FX provenance do not expire with market history.

| Data | Retention / bound |
| --- | --- |
| Real trades and saved transaction FX | Durable audit history |
| Intraday observations | Minute buckets; roll up at session close +10 minutes, then delete minute detail |
| Daily price summaries and portfolio progress | Three calendar months |
| Decision snapshots | Scheduled: three months; manual: seven days / 32 records; 256 KiB per packet |
| Market outlooks | Scheduled: three months; manual: seven days / 32 records; 64 KiB per packet; assessments expire with parent |
| Company advice | Three calendar months, 256 records, 64 KiB each; count limit rejects new records rather than evicting unfinished forecasts |
| Company assessments / endpoints | Assessments expire with advice; shared endpoints survive pending consumers and paper marks; 16 KiB/4 KiB per packet |
| Paper run / decisions / fills | Retained together for replay; 10,000 decisions or 16 MiB immutable JSON per run; 16 KiB per decision/fill, 64 KiB per batch; cap pauses processing before partial capture |
| Paper FX observations | Seven days after pending orders terminate; consumed FX is copied into durable fills |
| Legacy news / signal history | Existing bounded per-symbol/global caches and historical expiry; strategy inputs unchanged |
| Presentation event audit | 14-day target; 1,000 evidence records globally / 100 per ticker, 250 roots / 4 revisions each; 8 KiB evidence / 4 KiB revision; 16 MiB combined serialized budget |
| News delivery identities / aliases | 30 days, 2,000 compact keys combined; evidence expiry never deletes an identity |
| UI tokens | 256 opaque owner/message-bound tokens, 24 hours |
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

New images are built on the Pi's architecture. The installer requires at least
1 GiB free in container storage before a new build, and reuses
an existing image only when its source-content release hash matches exactly.
Builds avoid retaining intermediate layers, which duplicate entire root filesystems
with the Pi's VFS storage driver. Older tagged releases still consume disk space;
inspect them before removing obsolete images, preserving current/previous images
and data volumes. `scripts/inspect-server.sh` includes filesystem free space.

The installer backs up a running prior database,
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

The direction is **better evidence and small local classifiers, with zero recurring
API cost**. Keep Python and the existing Pi. News classification answers “what
happened, to which company, and how important is it?” Forecasting answers “what
happens to the price afterward?” Better classification alone does not establish
better forecasts. Current outlook/advisor/paper rules have no established
predictive advantage.

The tested Needle classifier is not an integration candidate: it supplied no
accepted labels in the 200-news trial and was too slow in the tested Pi setup.
That result concerns this model/configuration, not every possible local model.
News digests, event rules, navigation and outcome collection already exist;
the following records implementation status and next priorities.

1. **Evaluate the revised classification rules on later news.** Rules now consider
   supported body sentences, missing financial/legal predicates, direct versus
   indirect context and scoped uncertainty. On the reused 200-article development
   sample, high-materiality detection rose from 4/13 to 8/13, but relevance agreement
   fell from 184/200 to 174/200. This is a tradeoff, not a general accuracy claim.
   Check a later untouched sample with related-event articles kept together;
   judge important-event recall, attribution, abstention and false alerts separately.
2. **Collect usable learning and outcome data in parallel.** Preserve source text,
   reviewed event labels, exact decision-time inputs and subsequent price outcomes.
   Improve comparable earnings and corporate-action evidence where it affects
   outcome validity. Define a bounded offline archive before existing retention
   caps discard useful training history; do not silently enlarge Pi storage.
   The current news references are assistant-reviewed, not expert ground truth;
   review ambiguous and important cases before using them for training.
3. **Revisit learned news classification after improving data.** A first offline
   scikit-learn TF-IDF/logistic-regression trial did not justify integration:
   grouped out-of-fold relevance was 127/200 versus the rules' 184/200; high-event
   detection was 2/13 versus 4/13. These are development-corpus results with sparse
   assistant-reviewed labels and uneven event groups, not independent validation.
   Gather more reviewed event families and explicit issuer context before another
   candidate. Train offline; export bounded vocabulary/weights only after a useful
   measured improvement and inference-parity/Pi-footprint checks. Keep the runtime
   training stack off the Pi and preserve rules/abstention as the fallback.
   Additional fixed trials of a small pretrained NLI encoder and a structured
   decision tree also failed the useful recall/false-positive tradeoff. The next
   proposed learning task is event/participant/status extraction, using reviewed
   current examples and an expert-labelled resource such as
   [SENTiVENT](https://huggingface.co/datasets/GillesJacobs/sentivent), with explicit
   materiality/alert policy afterward. This path is not yet trained or validated.
   See scikit-learn's [text feature documentation](https://scikit-learn.org/stable/modules/feature_extraction.html).
4. **Evaluate prediction changes separately, after outcomes accumulate.** First
   report current forecast coverage, errors and company returns relative to SPY.
   Then compare a small statistical candidate with frozen existing rules and simple
   baselines using only facts available at issue time. Train on earlier sessions,
   evaluate on later sessions, and exclude overlapping five-session outcome
   windows across the boundary. Freeze any new price-history features before use;
   old snapshots cannot supply history they never captured. Report uncertainty;
   a few sessions or many correlated articles cannot establish predictive skill.
5. **Promote only a demonstrated improvement.** Run a promising forecast candidate
   alongside the existing baseline as a separate prospective experiment. Preserve
   historical records and paper-rules-v1. Evaluate paper returns after fees/FX,
   turnover and drawdown against a separately specified portfolio benchmark.
   If the candidate fails to improve the relevant held-out/prospective results,
   retain the baseline and report that finding.

The immediate deliverable is the focused rule improvement in step 1, with a
before/after classification comparison. Forecast outcome collection continues
alongside it. Paid models, a Rust rewrite, new hardware and automated brokerage
execution are outside this plan. Further model experiments are conditional on
data and measured need, not an open-ended search for a model name.
