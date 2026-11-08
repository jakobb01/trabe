# 0.8.2 — earnings results fallback and insider coverage

**Superseded for earnings:** the owner challenged the Alpha Vantage VST EPS below.
That comparison was withdrawn and ingestion disabled. The HTTP/parser checks did
not establish financial accuracy. See [0.8.3](release-0.8.3.md) for the correction
and free-source replacement. Insider and command changes remain applicable.

VST's running 0.8.1 service successfully refreshed Finnhub, but the calendar still
had null Q3 actuals and its history stopped at Q2. The existing Alpha Vantage key
returned the missing Q3 EPS and income statement. No new subscription or key is
required for this fix. Background/manual refreshes now use those endpoints when
recent tracked releases lack figures.

EPS keeps Alpha Vantage's actual and its own estimate together. The live VST
check returned **1.75 vs 2.94**, a miss; it is not compared with Finnhub's 2.991
estimate. The fallback must match the exact reported release date, then uses its
fiscal-end identity to match revenue. Conflicting schedules, ambiguous matches,
future releases and mismatched symbols remain unavailable. Primary figures take
precedence when supplied later. Existing result alert claims prevent repeats.

Income-statement revenue is an ungraded actual: the endpoint supplies no paired
revenue estimate. VST's check returned **6347000000**, but its currency field was
`None`; messages explicitly show currency unavailable. No currency is inferred
from another quarter. These limitations remain in frozen comparison provenance.

Fallbacks share the existing Alpha Vantage budget, process lock, pacing and
cooldown. Automatic retries are at most every six hours per missing metric;
manual refreshes reuse a successful response for one hour. Only eight compact
quarterly records per endpoint are cached. Missing keys, errors and exhausted
allowances retain cached figures and leave source errors visible.

The insider adapter previously discarded everything after the first 100 Finnhub
transactions, even when a response contained more (GOOGL returned 174). It now
keeps bounded responses and splits crowded ranges, with a maximum of seven calls
and 2000 unique transactions per symbol per refresh. Unresolved crowded ranges
and failed subrequests remain explicitly partial. Role, transaction type,
non-derivative verification, threshold, amendment and deduplication rules remain
the qualification contract.

Reports now show the 90-day window, source purchase/transaction counts, check
time, eligibility counts and coverage gaps. The live check found three VST P
purchases in both sources and zero GOOGL P purchases in both; a healthy empty
purchase feed no longer reads like a source outage. `/insider` is an alias for
`/insiders`.

Bare `/earnings`, `/insiders` and `/news` (or a ticker argument) request a refresh
and deliver the report afterward. `cached [TICKER]` provides immediate local
views. Automatic feature switches remain separate from manual requests. SQLite
schema remains 7; credentials and ledger data require no migration.
