# 0.8.0 — company news, second assessments and daily snapshots

## 0.8.1 refresh performance fix

The Pi reported refreshes taking over five minutes. A bounded five-ticker fixture
with 750 supplied articles reproduced 35.7 seconds of computation on the development
machine, excluding network waits. Caching story signatures, avoiding repeated
issuer checks, reusing exact-input analyses, and grouping/pruning once per refreshed
ticker reduced the same fixture to 1.4 seconds. This is a local comparison, not
a measured Pi latency guarantee; API pacing and network waits still apply.

News/briefing jobs now commit progress one ticker at a time. `/status` separates
pending and running jobs and shows completed ticker counts. `/news sources` exposes
the sanitized stored error detail. Completed jobs clear their remaining work.
Focused checks passed: 30 news cases, 13 shared-control cases and 14 snapshot cases.
The extra analysis-cache index is compatible with schema 7; no new data migration
or runtime dependency is required. Deploy using the existing script.

Implemented in the working tree. This goal does not deploy the Pi, start a local
Telegram poller, build an image or create a Git commit. It includes the existing
0.7.2 earnings/message work. Paper trades and forecasts remain later milestones.

## What changes in Telegram

`/news` now reports one aggregate per company instead of treating each headline
as a separate company view. It uses explicit issuer context, a bounded financial
rules analyser and conservative story grouping. Copies do not multiply the signal
or reset its age. Different figures, periods and directions prevent near-merges;
explicit correction relationships supersede earlier claims after receipt.
Missing or insufficient evidence gives **No clear signal**, not Neutral (0.0).
Mixed outcomes remain flagged even when their net bias is zero.

Finnhub and Yahoo fallback remain available. Massive adds an independently
supplied sentiment assessment; Marketaux is an optional numeric adapter. Each
has its own persisted budget and cooldown. The local aggregate always uses the
same rules method across feeds. Vendor assessments stay separate: the app does
not average incompatible scales or interpret a categorical label as +1.0 strength.
The second view compares direction on directly relevant matched stories and
shows the matched count, disagreement or unavailable data. Different text scope
is explicit. Default news messages continue to omit source links.

```text
🟢 VST • Bullish (+0.5)
2 distinct stories
Vistra raises guidance • 1h ago
Second view: agrees on direction • 2 matched stories
Second view uses different text scope.
```

The example is invented. Sentiment remains a heuristic interpretation of supplied
text. No model is installed or selected, and the score is not a return forecast
or calibrated confidence. The small analyser protocol and versioned storage are
ready for a separately evaluated model later.

## Configure and try the features after deployment

The private `.env` accepts `MASSIVE_API_KEY` and optional `MARKETAUX_API_KEY`.
The existing deployment script now carries both into the private container
environment; pairing also preserves them. Keys are never configured through bot
messages. Ordinary preferences stay in Telegram.

Massive access was checked on 2026-11-04 using the owner's key and a temporary
database. The recent-window request returned **62 GOOGL articles and 3 VST
articles**, all with matching ticker sentiment. This verifies those requests;
it does not establish complete coverage or sentiment accuracy. Redacted evidence:
`.local/news-08-probe/access.json`. No Marketaux account was checked or enabled.

The default source list remains `finnhub,yahoo`. After deploying, one relevant
Telegram walkthrough is:

```text
/set news_sources finnhub,yahoo,massive
/news refresh
/news VST
/news sources
/snapshot now
/snapshot latest
/snapshot on
```

`/news sources` shows configuration, returned-item/coverage states and daily
usage. Initial app budgets are Massive 200 attempts/day, at most four/minute,
and Marketaux 80/day with at most six checks per ticker/day. Cache intervals are
one hour and three hours respectively. Manual refresh does not bypass quotas.
These are minimum intervals; worker load, source failures and budgets can delay
collection. Only enable Marketaux after saving its own key and checking access:

```sh
python3 scripts/check_news_access.py --provider marketaux
```

This check never contacts Telegram or changes the runtime database. The Massive
variant is `python3 scripts/check_news_access.py` and has already passed for the
current key; another run is unnecessary unless the key or entitlement changes.

## What a snapshot saves

A decision snapshot is an immutable input packet, separate from database backups
and the existing portfolio-progress history. It copies the committed positions,
basis/value/profit, FX, chosen prices and baseline/coverage flags, local news view,
bounded evidence and vendor assessments, earnings expectations/results/history,
qualified insider facts, source statuses, settings and data gaps.

Money uses decimal strings. One short SQLite transaction supplies all readers;
capture does not use the monitor's unpersisted quote, fetch data, cluster stories
or run inference. It records the actual capture cutoff after acquiring the write
transaction, plus the scheduled time and lateness. Previously received session
prices can be valid pre-open references; they are not executable fills.

Later article edits, second scores, estimate revisions, currency changes,
backdated trades or FX completion affect new packets. They cannot update or
replace an existing one. Canonical JSON has a SHA-256 integrity hash; database
triggers reject updates and replacement inserts. Retention can delete whole
eligible records. Evidence is copied, so ordinary feed-cache expiry cannot erase
the explanation of a saved packet.

| Control | Behavior |
| --- | --- |
| `/snapshot` or `/snapshot latest` | Show the last saved packet and current schedule/status. |
| `/snapshot now` | Save cached inputs now; no provider calls. Limited to once/minute. |
| `/snapshot YYYY-MM-DD` | Read that US session's automatic packet or missed/expired status. |
| `/snapshot on` / `/snapshot off` | Enable/disable silent automatic capture and its preparation job. |
| `/set snapshot_time preopen-30m` | Default: 30 minutes before a verified US session opens. |
| `/set snapshot_time 08:00` | Owner-local time on the date of that session's open. |
| `/set news_second_view off` | Hide the optional cross-check line without deleting stored assessments. |

Capture defaults off. The existing maintenance thread checks its schedule about
every 30 seconds; there is no additional bot poller. Ten minutes before capture,
one automatic job prepares news through existing budgets. Feature changes, pause,
storage pressure and the deadline cancel obsolete preparation. Explicit manual
refresh jobs retain their existing behavior. The capture uses ready data, marking
gaps if preparation did not finish. Five minutes of labelled lateness are allowed;
later runs are marked missed without backfilling from newer data. Automatic
capture is unique per US session and sends no notification. Briefing and snapshot
times are independent, and `/status`/briefing include concise snapshot status.

## Migration and bounds

Schema **5 → 6** creates six news tables and imports old extracts as
`legacy_extract`, observed at migration time. It cannot recover old full snippets
or classification history. Old `news_daily` rows remain read-only until expiry.
Schema **6 → 7** adds decision packets and scheduling claims. Both steps run in
the normal atomic Store upgrade. Existing ledger, price, FX, watchlist, polling
offset, preferences and pending replies are preserved.

Rollback to 0.7 requires restoring its pre-deploy database backup, because it
cannot read schema 7. The existing deployment rollback already restores that
backup. A saved decision packet is not a substitute for that database backup.

| Data | Limit |
| --- | --- |
| Current news articles | 20 per ticker; 1,000 globally. Distinct event representatives get slots before extra copies. |
| Content revisions | 5,000; headline 512 characters, snippet 4,096. |
| Provider observations | 10,000; at most 4 KiB of entity annotations each. |
| Target associations / analyses / event memberships | 10,000 each; individual analyses at most 8 KiB. |
| Automatic decision packets | Three calendar months; maximum 256 KiB each. |
| Manual decision packets | Seven days, at most 32 retained; maximum 256 KiB each. |

News also expires through the three-month exchange-local cutoff, including a
publication that is old despite a recent fetch. New cache/context/source states
expire too. Limits apply across providers, not separately to each vendor. A large
packet trims optional evidence explicitly, then fails if essential content still
does not fit; it never silently drops holdings. Source/input caps and partial
coverage remain visible. Existing durable ledger and backup policies remain.

Serialized-packet sizing and focused verification are recorded in
[validation](validation.md#08-news-and-snapshots). SQLite pages/indexes/WAL, news
caches and backup copies add storage beyond those JSON payload measurements.

## Review and commit

The implementation checklist is [todo08-news-snapshots.md](../todo08-news-snapshots.md).
See [provider research](research-news-providers.md) and the
[design](news-and-snapshots-plan.md) for rationale and later model options.

Suggested commit message:

```text
feat: add news cross-checks and immutable decision snapshots

Group company news with target-specific rules and independent Massive assessments;
add optional Marketaux scores, bounded provenance and per-source budgets.

Freeze positions, prices, FX and event/news inputs in immutable daily snapshots
with Telegram controls, session scheduling and bounded retention. Preserve the
pending earnings expectations, history and result-delivery improvements.
```
