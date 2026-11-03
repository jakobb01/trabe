# trabe 0.3: intraday monitoring and dip alerts

Implemented and deployed on 2026-11-03. This preserves the original release
contract; measured results are in [validation](validation.md). Its initial-release
acceptance checklist is historical. Routine development follows the smaller
[testing policy](../CONTRIBUTING.md#testing-policy).

Bring the provider boundary and timestamped price storage forward into **0.3**.
Use a free US intraday feed to make alerts useful during the trading session.
See the sourced comparison and capacity estimates in
[market-data research](market-data-research.md).

The selected provider is **Finnhub**, subject to verifying a real free key against
GOOGL and VST. Use one shared WebSocket for continuous trades, with bounded
REST calls for initial quotes, previous closes, session information and recovery.
Alpaca, Tiingo and Twelve Data remain documented for future use; their adapters
are outside 0.3. A Finnhub access failure blocks activation and reports the reason.

Alpha Vantage remains an explicitly labelled end-of-day option. An intraday feed
outage must not silently turn an old daily quote into an intraday alert input.

### Telegram controls

| Command | Behavior |
| --- | --- |
| `/provider` | Show the configured provider, feed capabilities and availability |
| `/provider finnhub` | Select the provider only when its credentials/capabilities are verified |
| `/monitor on`, `/monitor off` | Enable/disable automatic intraday collection and alert evaluation |
| `/alerts` | Show thresholds, overrides, pause state and recent alerts without fetching prices |
| `/history VST 5` | Show recent daily observed-price summaries and coverage labels |
| `/storage` | Show DB/WAL/backup sizes, retention cutoff and maintenance state |
| `/alert VST -5` | Override VST's percentage-drop threshold relative to its validated previous close |
| `/alert VST default` | Restore the global threshold |
| `/alert VST off` | Disable proactive VST alerts without removing the stock |
| `/pause 24h`, `/resume` | Pause/resume automation while keeping manual commands available |

All ordinary configuration stays in Telegram. Add provider credentials locally
to the private bootstrap environment; never request API secrets in chat. New
automation defaults off. A previously enabled daily summary keeps its setting,
separate from intraday monitoring.

`/status` should expose connection state, the latest source observation, market
session, subscribed/unsupported symbols and quota/cooldown state. `/quote` and
`/prices` must distinguish fresh intraday data, cached data and end-of-day data.
The existing daily summary remains optional and must describe its actual inputs.

### Data and alert contract

* Scope the first integration to supported US-listed stocks during the normal
  exchange session. Handle holidays, early closes and America/New_York DST;
  the user's timezone controls message display, not exchange opening times.
* Store provider/feed, symbol, currency, source UTC time, receipt time, session
  date, price and previous-close provenance. Never treat receipt time as proof of
  source freshness. Preserve last-trade versus derived-reference-price semantics.
* Calculate `(current_price / previous_close - 1) * 100` only when price and
  baseline are compatible and validated. Do not reuse Alpha Vantage's cached
  percentage with another feed's price. Missing or suspect baselines disable that
  rule; corporate actions must not manufacture a large price-drop alert.
* Maintain a bounded ingestion queue and latest-price cache. Evaluate eligible
  incoming observations promptly; persist minute observations/summaries rather
  than every tick. Retain minute detail only for an unfinished session; finalize
  it into daily summaries after the close, then remove the minute rows. Any
  observed high/low is specific to the received feed and cannot be presented as
  complete full-market OHLCV.
* Preserve a breach detected within a minute even if the price recovers before
  the minute sample is written. Backpressure/reconnect gaps must be visible;
  resuming a connection does not prove every intermediate trade was observed.
* Trigger at most one proactive event per symbol/rule/exchange session in 0.3.
  Keep repeated flags visible in manual scans without repeatedly notifying the
  owner. Multiple threshold levels and rearming after recovery can come later.
* Reject invalid, future, duplicate, out-of-order, stale and out-of-session data
  for alerts. Start with a documented 120-second maximum source age during the
  core session, configurable through Telegram; validate this default against the
  chosen feed. A quiet symbol may become unavailable for alerts instead of being
  described as current. A fresh stream heartbeat does not make every symbol fresh.

### Storage decision

Keep approximately 3,900 minute buckets/day for a stable 10-stock watchlist while
the session is open. At the actual close plus ten minutes, atomically roll up to
one daily row per stock/feed and delete that session's minute detail. Keep the
daily rows for three calendar months, then expire them.

The [storage plan](storage-and-retention.md) defines the daily fields, exact cutoff,
crash recovery, bounded backups and measured disk usage. A synthetic SQLite
measurement used about 0.44 MiB for 10 stocks' active minute data plus retained
daily history; the packed archive alone was about 0.10 MiB. WAL and backup files
raise the measured peak to about 6.23 MiB. Reserve 32 MiB for a typical 10-stock
workload and 64 MiB for 50 stocks, subject to verification on the actual Pi.

These are planning allowances, not hard WAL limits. Historical intraday charts
will not be available after daily rollup. `/history` presents the retained daily
summaries with provider and coverage metadata.

### Implementation slices

1. **Prove free-account access.** With a key entered locally, validate GOOGL/VST
   REST fields, baseline, timestamps and streaming subscriptions while the market
   is open. Confirm subscription limits, session/status endpoints and actual feed
   characteristics. Check the closed-market case too. If an account lacks access,
   report it clearly before enabling monitoring; do not subscribe to a paid plan.
2. **Provider/configuration boundary.** Separate Telegram delivery from market
   adapters. Add normalized observation/capability contracts and provider-specific
   credentials, request accounting and cache namespaces. Keep the Alpha Vantage
   budget intact; its 20/day default and 25/day setting cap cannot be reused for
   Finnhub. Update private credential serialization and the deployment bundle so
   the new key survives deployment without appearing in logs or image layers.
3. **Session-aware ingestion.** Add one outbound WebSocket worker, subscription
   reconciliation after watchlist changes, bounded queues and reconnect/backoff.
   Use REST sparingly for bootstrap/recovery and an explicitly validated calendar
   for session gating. Do not silently ignore symbols above the free plan's
   subscription limit. Keep `/status` responsive while the stream reconnects.
4. **Observation storage and migration.** Add numeric minute buckets, daily
   summaries, normalized instrument/feed metadata, effective alert settings and
   a persistent pause timestamp. Batch closed-minute writes in one transaction
   per minute. Preserve watchlist, preferences, existing API usage, scan history
   and pending replies. Recover unfinished buckets before the next session.
5. **Rollup, retention and storage reporting.** Implement an idempotent daily
   rollup/delete transaction, close-time grace period, three-calendar-month expiry
   and startup catch-up. Run maintenance even while monitoring is off/paused.
   Reuse freed SQLite pages, checkpoint the WAL when quiet and report busy-reader
   failures. Add a shared backup-protection lock/marker and prune automatic
   snapshots to three retained plus one in progress, with the same age limit.
   Implement `/history` and `/storage`, disk-reserve monitoring and a safe pause
   when continued growth or low space threatens persistence.
6. **Transactional alerts.** Evaluate the previous-close rule using a pure rule
   module. Claim a unique symbol/rule/session event and enqueue its message in
   one SQLite transaction. Save the price, timestamp, feed, baseline and threshold
   used; persist the triggering bucket with the event when needed to retain a
   brief breach through a crash. Threshold edits, provider changes and remove/re-add operations must not
   resend an existing session event. Changing providers invalidates cached
   baselines and requires a fresh compatible observation before evaluation.
7. **Telegram controls and delivery.** Implement the proposed commands, menu and
   status. Recheck pause before automatic provider work and inside the enqueue
   transaction. Tag proactive outbox messages so pause suppresses them without
   blocking control replies. Resume the current session without a historical alert
   backlog. Bound delivery latency independently of market-data ingestion and
   respect Telegram pacing.
8. **Release on the Pi.** Test upgrade/rollback using a populated previous
   database. Verify the WebSocket dependency installs and runs on ARMv7 in the
   production image. Deploy through the existing script, retaining readiness,
   restart-persistence, credential isolation and no-published-port checks.

A free feed can be useful without representing every US venue. Messages should
name the feed and source time. This release monitors the data actually received;
it does not promise to catch every market tick or match a consolidated broker
screen exactly. The existing Telegram lost-response retry limitation also remains.

### Acceptance criteria

* A real free account supplies usable GOOGL/VST updates during an open session.
  Timestamp age, baseline and feed entitlement are recorded in the acceptance log.
* An eligible breach creates one event and one queued notification. A brief breach
  followed by recovery within a sampling minute is still retained and alerted.
* Duplicate ticks, repeated scans, restarts, threshold/provider edits and
  remove/re-add operations do not duplicate the session event.
* Concurrency and crash tests leave both the alert ledger entry and outbox record,
  or neither. Delivery retries make no extra market-data request.
* Stale source timestamps, malformed frames, future clocks, unavailable baselines,
  split-related inconsistencies and mixed-feed inputs produce no fresh alert.
* Reconnect/backoff works without multiple simultaneous sockets for the same key.
  Subscription denial and rate limiting remain distinguishable from invalid symbols.
* Exchange holidays, early closes, DST mismatches, process downtime and expired
  calendars do not cause an unbounded polling loop or off-session alerts.
* Provider quotas are independent and persisted appropriately. Cache reads and
  local history/status commands consume no provider requests.
* Pause survives restarts, suppresses proactive delivery and stops the next
  automatic request/subscription work. Manual commands remain usable.
* Daily rollup preserves observed extrema, baseline and partial-session metadata,
  then removes minute detail atomically. Repeating it cannot erase a summary.
* Three-month expiry is correct at month ends/leap years, after downtime and after
  backup restoration. Monitoring pause/off does not stop maintenance.
* A multi-session soak test demonstrates page reuse and bounded database/WAL
  growth. Backup pruning respects active rollback, count and age limits.
* Retention, queue bounds and reconnect load fit the existing Pi resource limits.
  `/storage` reports failures and low-space collection pauses without consuming
  market API requests. Upgrade/rollback preserves data and opens no listening port.

### Where the work belongs

| Component | Change |
| --- | --- |
| `trabe/providers/` (new) | Normalized contracts, Finnhub and existing Alpha Vantage adapter |
| `trabe/monitor.py` (new) | Session-aware stream lifecycle, watchlist subscriptions and bounded ingestion |
| `trabe/alerts.py` (new) | Pure threshold evaluation and event decisions |
| `trabe/store.py` | Migration, numeric minute/daily tables, provider usage and unique alert events |
| `trabe/maintenance.py` (new) | Transactional daily rollup, expiry, checkpointing, storage metrics and catch-up |
| `trabe/outbox.py`, `runtime.py` | Transactional enqueue, proactive-message tags and responsive delivery |
| `trabe/bot.py`, `preferences.py`, `scans.py` | Telegram controls, capability-aware quotes/scans and pause |
| `trabe/config.py`, `setup.py`, deployment scripts | Private key setup, active-provider checks and protected/bounded backups |
| `tests/` | HTTP/WebSocket fixtures, rule/transaction tests, session cases and populated-database upgrade |
