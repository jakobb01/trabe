# Insider coverage checkpoint — 2026-11-08

The read-only Pi check confirmed 0.8.4 and healthy recent Tracefour/Finnhub
coverage for the five current tickers. It used saved runtime records, without
new provider calls or another Telegram poller. Evidence:
`.local/earnings-current-probe/insiders-review-before.json`.

| Ticker | Tracefour purchases | Finnhub P / total transactions | Qualified cached purchases |
| --- | --- | --- | --- |
| VST | 3 | 3 / 9 | 3 |
| GOOGL | 0 | 0 / 172 | 0 |
| AMD | 0 | 0 / 144 | 0 |
| AMZN | 0 | 0 / 60 | 0 |
| NVDA | 0 | 0 / 38 | 0 |

All five had successful source checks and no partial-range flag. Historical AMZ
source bookkeeping also exists; it is separate from the current AMZN coverage.
These observations give no evidence of a missing purchase on the current
watchlist and do not establish complete market-wide coverage.

## Local gaps found and fixed

- Primary purchase storage still discarded records after row 100, independently
  of the earlier Finnhub verification fix. Retain up to the existing primary
  response bound of 500 per ticker; keep the global 5,000-row/three-month limits.
- New snapshots inspected only 100 insider rows before filtering eligibility.
  Inspect the bounded 500-row set so excluded records cannot hide a later
  qualifying purchase. The final three-fact/payload limits and old packets stay
  unchanged; no new snapshot fields or schema migration are needed.
- A successful Finnhub refresh overwrote earlier Tracefour coverage details
  when Tracefour failed. Preserve independent source counts/check times, show
  each source's freshness, and retain the actual filing receipt time separately
  from the later transaction-verification receipt.
- Invalid filing rows were silently omitted from the coverage summary. Show an
  unusable-row warning; calculate displayed eligibility from the retained records
  and current threshold instead of a possibly outdated saved count.

Regression cases reproduced the storage, source-outage and receipt-time bugs
before the fixes. Focused verification: 21 insider cases, 15 snapshot cases and
changed-file Ruff checks passed. The 0.8.5 rollout and combined live walkthrough
passed: a fresh VST report retained three eligible purchases with separate source
receipt times, no unusable rows/partial coverage and successful delivery. See the
[release evidence](release-0.8.5.md).

## Remaining limits and source decision

No additional API is needed for these fixes. Tracefour still caps responses at
500 and accepts only 1–5 letter tickers. Finnhub verification remains bounded to
seven requests/2,000 unique transactions per ticker; crowded/failed ranges remain
explicitly partial. Direct SEC access was previously blocked. Unknown role or
non-derivative status continues to prevent a qualified alert. If a concrete
missing filing or unsupported share class becomes relevant, compare that exact
case and report the free-source gap before choosing another adapter.
