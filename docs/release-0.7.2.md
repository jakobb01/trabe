# 0.7.2 — earnings expectations, quarterly history and result updates

`/earnings VST` now combines the next expected release, expected EPS/revenue,
and up to four recent quarters of EPS actual versus expected. `/earnings` shows
the same expectations with a compact historical verdict per held/watched stock.
All views use cached data; `/earnings refresh` requests new data in the background.
Earnings messages remain free of source links.

An illustrative view:

```text
📅 Earnings

VST • 2026-11-06 • in 2 days
before-open • FY2026 Q3
Expected: EPS 2.991 • Revenue 6.08B

Recent quarters • EPS (actual vs expected)
FY2026 Q2 • 🔴 MISS • EPS: 0.76 vs 1.6938 expected (-55.1%)
FY2026 Q1 • 🟢 HIT • EPS: 2.87 vs 1.3526 expected (+112.2%)
```

These example figures came from the provider check on 2026-11-04, not a promise
that expectations or schedules remain unchanged. Source metadata stays in storage.

## Hit, met and miss

- **HIT**: reported actual is higher than the estimate (a beat).
- **MET**: actual equals the estimate.
- **MISS**: actual is below the estimate.
- Missing estimates or actuals remain unavailable; they never become zero or MET.

EPS and revenue are graded separately. An EPS hit and revenue miss stay distinct
rather than being presented as one overall company outcome. Surprise percent is
`(actual - estimate) / abs(estimate) × 100`; this also handles negative EPS
estimates. A zero estimate has no defined percentage. Comparisons use Decimal
arithmetic, with display rounding only.

Figures use the provider's reporting units; earnings amounts are not converted
to the portfolio's EUR/USD default. No GAAP/adjusted EPS reconciliation is
invented. When historical EPS supplies a result, it uses that endpoint's paired
estimate, not an estimate from another dataset. Historical revenue comparisons
are not available from the selected free EPS-history endpoint.

## Updates during a release

The existing **`/earnings on`** switch enables both upcoming warnings and result
updates. No additional account, key or port is needed. With that switch off,
manual views/refreshes and an independently enabled briefing still work.

Normal calendars/history refresh once a day. For a known pending release:

- Before-open results: checks accelerate two hours before the verified US open.
- After-close results: checks accelerate thirty minutes before the verified close.
- During-session/unknown time: checks start two hours before the open.
- Checks run about every **five minutes**, until 22:00 New York time. Incomplete
  results get two following days of checks between 07:00 and 22:00, then ordinary
  daily refreshes. Existing rate limits, daily budgets, feature switches, global
  pause and shutdown gates still apply.

As soon as comparable actual figures appear for a release already being tracked,
trabe sends an update such as:

```text
📊 Earnings update • VST • FY2026 Q3
🟢 HIT • EPS: 2.5 vs 2 expected (+25.0%)
Revenue: awaiting provider results.
```

If revenue arrives later, its new result is sent separately without repeating EPS.
When both arrive together, they share one message. Durable claims use the existing
atomic outbox path and survive restarts, settings edits and remove/re-add. Loading
historical data for the first time does not send four old result alerts. Activation
and resume suppress result observations already seen before their watermark.

This is polling of provider updates, not a live earnings-call stream. Publication
can be delayed or incomplete; no instant-at-announcement delivery is guaranteed.
Manual reports show pending/unavailable fields and keep known figures during an
outage. Corrections to a metric already notified update cached views without
another automatic alert; there is no repeated stream of estimate edits.

## Sources and storage

The existing free Finnhub key returned HTTP 200 for VST and GOOGL on
`/stock/earnings?limit=4`. The VST calendar returned upcoming EPS and revenue
estimates. API models:
[historical EPS fields](https://github.com/Finnhub-Stock-API/finnhub-go/blob/master/docs/EarningResult.md),
[calendar fields](https://github.com/Finnhub-Stock-API/finnhub-go/blob/master/docs/EarningRelease.md).
The calendar's revenue expectations can include Finnhub's proprietary estimates;
they are provider expectations, not a claim about every analyst's consensus.

The shared Finnhub transport accounts for all calls. Historical EPS is a bounded
latest-four cache, not an accumulating multi-year archive: one signal per symbol,
a global cap of 1,000 such snapshots, and ordinary three-month inactivity expiry.
Fiscal year/quarter or an explicitly matching fiscal period identifies a release;
the release's calendar year is not assumed to be its fiscal year. Result observations
retain their first-seen time and source/basis. Schema remains **5**; no ledger,
dependency or credential migration is required.

## Rollout

Deploy with the existing `bash scripts/deploy.sh`. After its verification finishes,
send `/earnings VST`, `/earnings refresh`, and `/earnings on` as wanted. The first
message can queue a fetch. Check the expected figures and recent-quarter results;
a real result alert requires a new tracked release to become available.

Development used fixture data and saved private provider responses; it never
started another live Telegram poller or changed the Pi.
