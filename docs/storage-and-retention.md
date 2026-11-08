# Storage and retention for trabe

Price-source decision: 2026-11-03; news/snapshot bounds updated 2026-11-04. Finnhub WebSocket is the selected intraday source.
Other providers remain documented in [the research](market-data-research.md) for
future use. This document specifies the implemented retention policy; measurements
and deployment evidence are recorded separately in [validation](validation.md).

## News revisions and decision snapshots in 0.8

The new authority stores six bounded news tables: canonical articles, observed
content revisions, provider observations, ticker associations, analyses and event
memberships. Both provider variants and first-availability times survive URL
deduplication. Old `signals(kind='news')`/`news_daily` rows are legacy data and
are not dual-written by the new news service.

Limits shared across sources: 1,000 current articles / 20 per ticker, 5,000
revisions, and 10,000 each for provider observations, associations, analyses and
event memberships. An annotation bundle is at most 4 KiB and an analysis 8 KiB.
Inputs are bounded to 512 headline / 4,096 snippet characters. Distinct event
representatives get article slots before additional copies; cap-related coverage
loss is recorded. Old publication/observation data and derived cache/source state
expire through the three-calendar-month exchange-local cutoff.

`decision_snapshots` stores canonical JSON and its SHA-256 hash; `snapshot_runs`
holds scheduling bookkeeping. Finalized records reject updates/replacements.
Automatic records expire after three calendar months. Manual records expire
after seven days, have a global cap of 32 and a one-minute creation interval.
Each packet is at most 256 KiB; optional evidence trims explicitly before a
too-large capture fails. No holdings are silently dropped. These records copy
their compact evidence and do not cascade away when news inputs expire.

See [release bounds](release-0.8.md#migration-and-bounds) and
[measured fixture payload sizes](validation.md#08-news-and-snapshots).
The prior 0.7 cache contract follows for interpreting older releases/backups.

## Earnings, insiders and news in 0.7 (historical)

The 0.7 candidate adds bounded JSON signal records, not another tick archive or
full article collection. Event/publication time, first observation, availability
and source/method metadata support later forward advisor experiments.

| Signal | Additional row limit |
| --- | --- |
| News articles | 20 per symbol; 1,000 globally, deduplicated across symbols |
| Frozen daily news signals | 4,500 globally |
| Insider transactions | 100 per symbol; 5,000 globally |
| Earnings events | 2,000 globally |
| Latest-four EPS history snapshots (0.7.2) | One per symbol; 1,000 globally |
| Briefing snapshots | 100 globally |

The 0.8.5 insider coverage patch raises the per-symbol purchase bound from 100
to 500, matching the bounded primary response. The global 5,000-row and
three-month limits still apply. New snapshots inspect the retained set before
choosing up to three eligible facts; existing snapshot packets remain immutable.

Payloads are limited to 32 KiB each; normal records are substantially smaller.

0.8.6 adds a current market-context row per symbol (1,000 globally), plus one
latest market-news packet with at most ten stories/30 KiB. They use existing
signal retention. A shared parsed-article cache in `signal_http_cache` caps at
32 documents/128 KiB each, keeps at most 100,000 text characters per document,
refreshes after six hours and expires after seven days. Invalid/failed reads get
one-hour negative caching. Raw HTML and complete intraday chart arrays are not
archived. Source text extracts and their observation history remain in the
existing bounded news pipeline; these are not additions to immutable v1 packets.
EPS history snapshots deliberately contain the latest four quarters, including
older fiscal periods, with a current as-of time. Inactive snapshots expire after
three months; they do not create an accumulating four-quarter archive per day.
These row limits are ceilings, not storage estimates. The 0.3 measurements below
cover price tables only and must not be read as measurements of these new tables.

Compact news, insider, daily-signal and briefing history expires by event date
after three calendar months, even when an old item is fetched again. Other signal
metadata expires after three months without observation; a future earnings event
is preserved. Notification claims/source state expire after the same retention
window. Finished refresh jobs expire after one day, hourly request records after
one hour, and HTTP response caches after seven days. Refresh jobs are capped at
16 active requests. Existing disk-pressure controls and bounded automatic backups
still apply. Transaction records and saved transaction FX are never removed by
this signal cleanup.

## Selected policy

1. Process incoming trades in memory for live prices and alerts. Keep a bounded
   queue and a minute bucket per subscribed stock; do not archive every tick.
2. Persist minute buckets for the active exchange session. Approximately 10 stocks
   produce at most 3,900 buckets during a normal 390-minute session. Missing
   observations do not become artificial flat-price candles.
3. At the actual session close plus a ten-minute grace period, create one compact
   daily summary per stock/feed, then delete that session's minute buckets in the
   same SQLite transaction. Early closes use their actual closing time.
4. Retain daily summaries for **three calendar months**, then delete them. This
   replaces the earlier proposal to retain 30 days of minute observations.
5. Bound automatic database backups too: three retained snapshots plus at most
   one protected snapshot while deployment/rollback is in progress. Expire old
   automatic snapshots after three months. Manual backups remain user-managed.

This is aggregation rather than file compression. Historical daily prices and
ranges remain available; the old intraday path cannot be reconstructed after the
minute rows are removed. No gzip files or separate time-series service are needed.

## What survives each trading day

Store one row keyed by instrument, provider/feed and exchange-session date:

| Fields | Meaning |
| --- | --- |
| Observed first, high, low and last prices | Four price values from the eligible data trabe received |
| Previous-close baseline and its provenance | Basis for the percentage-change rule |
| First/last source timestamps | Distinguish a full observed session from a short fragment |
| Observed/expected minute counts and largest gap | Coverage and interruption diagnostics |
| Alert count and quality flags | Breaches and partial/missing/suspect data |

Provider, symbol, currency and feed metadata are referenced by numeric IDs rather
than repeated in every minute row. Daily percentage change and observed range
can be calculated from these stored values. The summary is **observed feed data**,
not an official exchange OHLC/closing price. Never invent a closing price for a
session with no usable data; keep missing/partial values and quality flags.

## Capacity calculation and measurement

A normal US core session has 390 minutes. For a conservative three-month model,
allow 66 weekday sessions with a stable watchlist; exchange holidays reduce the
actual count. Ticker churn and extra feeds add metadata/summary rows. For example,
the window after 2026-08-03 through 2026-11-03 contains 66 weekdays before
subtracting exchange holidays.

| Stocks | Minute rows during one full session | Daily rows over 66 sessions |
| --- | --- | --- |
| 2 | 780 | 132 |
| 10 | 3,900 | 660 |
| 50 | 19,500 | 3,300 |

I measured synthetic prices using the proposed numeric SQLite tables and indexes,
4 KiB pages, WAL mode, full synchronization and one transaction per minute. The
experiment rolls up a session, deletes the minute rows and fills the next session
to check page reuse. These are local measurements, not Pi performance results.

| Stocks | Active database: one day of minutes plus retained daily history | Packed daily archive only | Measured peak DB + WAL + shared-memory files | Peak including 3 backup copies and 1 in-progress copy |
| --- | --- | --- | --- | --- |
| 2 | 0.105 MiB | 0.039 MiB | 4.082 MiB | 4.503 MiB |
| 10 | 0.438 MiB | 0.098 MiB | 4.473 MiB | 6.223 MiB |
| 50 | 2.145 MiB | 0.371 MiB | 6.431 MiB | 15.025 MiB |

One MiB is 1,048,576 bytes. The archive column measures a separately packed copy
to show the size of the retained daily data. Routine operation reuses freed pages
in the main file; it does not produce that packed copy every night.

For the user's roughly 4,000-minute-row workload, the market-data tables are
therefore small. Reserve **32 MiB for a typical 10-stock deployment**, or **64 MiB
for the 50-stock case**, to leave room beyond this experiment for application
state, alert evidence, queued replies and schema changes. These are planning
allowances, not enforced hard limits or measurements of free space on the Pi.
Container images, journald logs and user-created backups are separate consumers.

The saved [measurement record](measurements/storage-estimate-20261103.json)
contains exact byte counts and exclusions. Reproduce it without credentials:

```sh
python3 scripts/estimate_storage.py --symbols 2 10 50
```

The schema in this script is a sizing model. Production changes to indexes,
columns, commit cadence or connection lifetime require rerunning the measurement.

## Physical disk behavior

Deleting rows frees SQLite pages for reuse; it generally does not shrink the main
file. In the 10-stock experiment, the file remained 458,752 bytes after rollup and
the next day's minutes reused those pages without growth. This is desirable:
retaining one day's working capacity avoids a database rewrite each night.
[SQLite space reclamation](https://sqlite.org/lang_vacuum.html)

The WAL was the largest temporary component in the small-watchlist experiment.
Keep transactions short, use automatic checkpointing during collection and request
a truncating checkpoint after quiet end-of-day maintenance. Retry a busy checkpoint
later; never delete live SQLite sidecar files manually. Long-lived readers can
prevent checkpoint progress, so the measured WAL peak is not a universal bound.
[SQLite WAL/checkpoint documentation](https://sqlite.org/wal.html)

Use numeric columns and composite primary keys for the new tables; the experiment
uses `WITHOUT ROWID` for minute/daily data. Keep an index for expiry queries and
measure the final schema. [SQLite table layout](https://sqlite.org/withoutrowid.html)

## Rollup and expiry rules

* A maintenance worker runs independently of whether monitoring is enabled,
  paused, receiving frames or connected. It also runs during startup recovery.
* Flush due minute buckets first. Persist the daily summary and delete the
  corresponding minute rows in one transaction. Mark the session finalized in
  durable state. A failure leaves the minute rows available for retry.
* Rollup is idempotent: a repeated run must not double-count data, overwrite a
  finalized summary with an empty aggregate, or affect notification deduplication.
* Recover unfinished previous sessions before beginning a new session's storage.
  If rollup cannot complete, pause collection with a clear status; do not accumulate
  another full day of unbounded minute data. Unknown/expired calendars also require
  a visible paused state, not a guessed midnight finalization.
* Preserve incomplete sessions as partial summaries. An interrupted connection,
  late activation or premature shutdown must remain visible in their quality data.
* Compute retention using the exchange-local date, independent of the owner's
  display timezone. Subtract three calendar months, clamping to month end when
  necessary; dates at or before that cutoff expire. On 2026-11-03 the cutoff is
  2026-08-03. Daily history after that date remains.
* Purge expired daily history, historical alert evidence and completed scan/run
  history. Preserve current-session deduplication, pending unexpired replies,
  watchlists, settings, credentials and active jobs. Expired abandoned minute rows
  can be deleted without constructing an already-expired archive.
* Run expiry after restoring a backup, before old history is shown or automation
  resumes. Old automatic backup files also expire; active rollback snapshots are
  protected until the operation completes.

Minute buckets normally commit once per minute, plus graceful-shutdown flushes.
A power failure can lose an unflushed partial minute. Record that gap instead of
claiming full coverage. An alert transaction must preserve its triggering price
and source time immediately; include the current bucket in that transaction when
needed so a brief breach survives recovery and daily aggregation.

## Operational bounds and Telegram visibility

Add `/storage` to show the allocated database, WAL and automatic-backup sizes,
minute/daily row counts, retention cutoff, last rollup/purge and any maintenance
failure. Add `/history VST 5` for recent daily summaries with source/quality labels.
Both commands use local data and spend no market API requests.

Expose the planning allowance as a status threshold and monitor actual filesystem
space. Warn once per continuing condition when it is exceeded. If free space is
below a reserve, initially 100 MiB, or checkpoint/maintenance failures cause
continued growth, pause collection while preserving control replies and recovery
information. The final reserve/thresholds must be checked on the Pi.

Bound debug logging and avoid logging every tick. Prune automatic deployment
backups after a completed deployment or rollback, never before a protected restore
point is no longer needed. The installer and application maintenance must share
a volume-local backup lock/protection marker so they cannot prune an active
snapshot. Release 0.3 implements this bound for automatically named snapshots;
older manual or differently named backups remain user-managed.

## Required verification before 0.3 ships

* Correct aggregation of normal, early-close, no-data and partial sessions;
  observed extrema survive minute sampling and brief alert breaches.
* Atomic rollup/delete and alert enqueue; restart/crash at each boundary does not
  lose the only copy, duplicate a summary or send an alert again.
* Three-month expiry across month ends, leap years and timezone changes;
  expired data does not reappear after restoration or paused operation.
* Restart after several offline sessions rolls up valid unfinished data and
  removes expired data before enabling the new session.
* Repeated session cycles keep allocated pages and row counts bounded; readers
  holding a WAL snapshot produce a visible maintenance issue and later recover.
* Backup pruning preserves active rollback, respects the count/age policy and
  leaves explicitly user-created manual backups alone.
* ARMv7 container load/soak test under the existing 256 MiB limit, including the
  50-symbol storage model. Record actual DB/WAL growth and disk-write behavior.
