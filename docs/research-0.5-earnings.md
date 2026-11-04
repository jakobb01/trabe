# Earnings watchdog: source research and implementation recommendation

Checked 2026-11-03 UTC (2026-11-04 local session). Research only: no Pi changes,
real Telegram polling, notification sends, paid accounts or live database changes.

## Sources and observed access

| Source | Authenticated observation with existing key | Recommendation |
| --- | --- | --- |
| Finnhub `/calendar/earnings` | HTTP 200. A filtered `symbol=VST` request returned one upcoming report, 2026-11-06, `bmo`, fiscal Q3/2026. NVDA appeared as 2026-11-17 `amc`, fiscal Q3/2027. | Primary, **with a symbol filter** and persistent caching. |
| Finnhub all-symbol calendar | Both a 30-day window and a seven-day window returned exactly 1,500 rows. VST was missing from the longer response but present in the filtered request. | Treat a full-looking all-symbol result as potentially truncated. Never infer absence from it. |
| Alpha Vantage `EARNINGS_CALENDAR`, `horizon=3month` | HTTP 200 CSV, 3,321 records. VST and NVDA dates agreed with Finnhub; the response also supplied `fiscalDateEnding` and an optional `timeOfTheDay` field. | A useful bulk fallback using the existing Alpha key, cached once/day. |

Private evidence is in `.local/validation/earnings-entitlements.json` and
`.local/validation/earnings-followup.json`. The probes used three Finnhub calls
and one Alpha Vantage call, without printing credentials or secret-bearing URLs.
These observations establish this account's current access, not a permanent
provider entitlement promise.

The official [Finnhub endpoint documentation](https://finnhub.io/docs/api/earnings-calendar)
and [Finnhub SDK response model](https://github.com/Finnhub-Stock-API/finnhub-go/blob/master/docs/EarningRelease.md)
describe report date, fiscal year/quarter, expected/actual EPS/revenue, and the
`bmo`, `amc`, `dmh` time categories. Fiscal year is not necessarily the report's
calendar year, as explained by the provider in
[its fiscal-year clarification](https://github.com/finnhubio/Finnhub-API/issues/437).

[Alpha Vantage's official documentation](https://www.alphavantage.co/documentation/#earnings-calendar)
specifies CSV, optional symbol filtering, and 3/6/12-month horizons. Omitting the
symbol returns the full calendar. `timeOfTheDay` was present in the live response
but should be optional in parsing because the documented schema does not promise
that field. Normalize `pre-market`/`post-market` to before-open/after-close.

## Minimal logic

1. Use `Ledger.coverage()` for the union of held and watched tickers. Fetch one
   symbol's upcoming calendar (today through the next 90 days) per refresh,
   caching successful results for 24 hours. Spread a large watchlist across the
   background worker; process manual `/earnings` as a cached view, not a 50-call
   foreground request. Empty data means unknown/no reported event in this window.
2. Retain normalized event identity separately from mutable report date/time.
   Finnhub's stable period key is symbol + fiscal year + fiscal quarter. Alpha's
   is symbol + `fiscalDateEnding`. Save provider, source URL without credentials,
   first/last observed time, current expected date/time category, period metadata
   and previous date when revised. Avoid inventing exact clock times or a
   company-confirmed status that neither feed supplies.
3. Refresh current entries; a changed date for the same fiscal key is a revision,
   not another quarterly event. A temporary empty response or outage must not
   erase the last known report. Mark stale/unavailable coverage visibly.
4. Default warning lead time to five calendar days, configurable 1–14 days
   through Telegram (including three days). A newly discovered event already
   inside the window gets one warning. Use exchange-local calendar dates for
   the report and day count, and the owner's timezone for message display.
5. Claim a warning and enqueue its outbox message in the same transaction. Key
   ordinary warnings by logical event, effective report date and warning rule;
   preserve claims through restarts, threshold edits and remove/re-add. A
   report-date revision gets a separate single correction keyed by old/new date.
   Do not send a fresh ordinary warning merely because its lead setting changed.
6. Maintain provider-independent notification deduplication for the same symbol
   and report date. On provider failover, associate matching ticker/date records
   with the existing logical event. Preserve fiscal identifiers as aliases. When
   dates/periods disagree and identity is uncertain, show both source claims as
   a conflict; avoid silently replacing a previously warned event or fabricating
   a date revision. Prefer a still-fresh primary result over an ambiguous fallback.
7. Alpha fallback can download one calendar/day and filter immediately to the
   covered symbols. Bound response size (8 MiB comfortably exceeded neither
   observed response) and field validation. Share Alpha's `.market.lock`,
   completion timestamp, cooldown and existing daily request budget. JSON error
   notices can arrive instead of CSV; classify quota/access denial rather than
   treating that body as an empty calendar. Never log its credential-bearing URL.
8. Share Finnhub's persisted minute/day allowance, header authentication, pacing
   and HTTP 429 cooldown. **Do not reuse `automatic=True` unchanged:** its current
   gate requires price monitoring on and Finnhub selected. Earnings/news should
   have their own enabled/pause gates and work independently of the price source.
9. Check pause and feature enablement before each automatic provider call and
   again inside notification enqueue transactions. Use the existing proactive
   `notice`/`summary` outbox kinds so pause filtering applies. Resume considers
   current upcoming reports, without replaying warnings for reports already past.
10. Include held quantity/exposure in warnings only from existing portfolio
    calculations, in the owner's currency and with missing/stale valuations
    labelled. A warning does not need a live quote to identify an upcoming report.

## Telegram surface

- `/earnings [TICKER]`: cached upcoming reports, date/time certainty, source,
  checked time and exposure where held.
- `/earnings on|off`: opt in/out of automatic refresh and warnings.
- `/set earnings_warning_days 5`: lead window (1–14 calendar days).
- Optional `/earnings refresh`: enqueue a budgeted background refresh; reply
  promptly and preserve cached results during outages.
- `/status`: source availability, last successful calendar refresh and quota or
  cooldown errors. Default proactive warnings off; manual cached view stays usable.

Suggested notification template:

```text
📅 Upcoming earnings • {ticker}
Expected: {report_date} • {before-open/after-close/during-session/unknown}
In {days} calendar days • provider-reported schedule
Held: {quantity or "watchlist only"} • exposure: {value/currency or "unavailable"}
Source: {provider} • checked {observed_at_utc}
{source_link_without_credentials}
```

A revision should explicitly say `Expected date changed: {old} → {new}` rather
than looking like another initial warning.

## Suggested storage and focused checks

Use compact tables for events/period aliases, date revisions and notification
claims, or extend a shared fundamentals event store. Keep first/last-seen and
source publication time separately; these APIs do not provide an authoritative
publication timestamp for every scheduled-date observation. Keep upcoming events
and claims for their lifetime; purge past event/revision data after three calendar
months. Do not expire a future event just because it was discovered months ago.

A few fixtures should cover: symbol-filtered parsing and missing time category;
same-period date revision and one correction; normal warning dedup through a
restart/lead edit; pause and empty/error response retaining cache; Alpha CSV vs
JSON quota notice; fiscal-year handling; and held-only symbol coverage. No ARM
soak, real chat messages or broad suite rerun is needed for this research.

## Limits to surface

- These are provider-reported expected dates, not guaranteed company confirmations.
- A report-time category is not an exact timestamp; unknown remains unknown.
- All-symbol Finnhub responses observed here were incomplete/truncated.
- Source disagreements must be visible; a fallback is not independent confirmation.
- Freshness of earnings schedules is separate from freshness of stock prices.
