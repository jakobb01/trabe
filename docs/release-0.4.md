# trabe 0.4: positions and progress

This release records completed transactions; it does not send brokerage orders.
Supported reporting and trade currencies are EUR and USD, for supported US-listed
USD stocks. The existing Finnhub feed supplies the observed USD stock price.

Transaction input is `/buy TICKER QUANTITY PRICE [CURRENCY] [YYYY-MM-DD] [fee=AMOUNT]`
and `/sell` with the same arguments. Currency defaults to EUR until changed with
`/currency USD`; date defaults to today in the owner's timezone. Quantities may
be fractional, prices and quantities must be positive, and fees are nonnegative.
The ledger stores original values, dates, fees, correction history and historical
FX provenance. Partial sales use weighted-average cost. A void or backdated entry
is rejected if replay would oversell at any subsequent point. `/trades TICKER`
shows IDs and `/trade_void ID` records a correction without erasing the original.

Use `/position TICKER` or `/portfolio` for cached quotes, remaining cost, market
value, unrealized and realized results. Incomplete prices or rates are labelled
and excluded from partial totals. Stale observations and closed markets are
labelled separately. Returns include stock and exchange-rate movements and fees.
The previous-close dip alert is separate from the entry-cost return. A flagged
baseline needs human review for a possible corporate action; no split is guessed.
For a confirmed event, `/position_adjust TICKER NEW_QUANTITY [YYYY-MM-DD]` keeps
the total basis while adjusting quantity. `/basis_adjust TICKER AMOUNT [EUR|USD]
[YYYY-MM-DD]` records an explicit remaining-basis correction. Both are auditable
and may be voided if later sale history remains consistent.

Frankfurter's [free daily reference rates](https://frankfurter.dev/v1/) update on publication days, usually
around 16:00 CET. The worker queries at most once per requested date per hour and
uses the last published rate on or before that date (including weekends and
holidays); rates older than seven calendar days are unavailable. A trade's
cross-currency rate is fixed after its execution date ends in UTC, allowing that
day's final reference publication to be used. Until then, or during an outage,
the original trade can be saved and affected conversions remain pending. Rates
are reference prices, not the broker's actual execution rate or spread. Each
saved conversion identifies its direction, provider, effective date and retrieval
time. Changing `/currency` does not change original trade amounts.
Failed historical requests back off without blocking later dates, and a pause
stops the next automatic FX request in a batch.

`/portfolio_summary 23:00` enables daily progress in the owner's timezone;
`/portfolio_summary off` disables it. Summaries use cached prices and rates,
report largest unrealized contributors, and compare unchanged positions from
the prior snapshot. Changed or unavailable positions are excluded, so capital
flows are not presented as market performance. A currency change prevents a
comparison across currencies. Pause suppresses automation, while manual reports
continue to work. Snapshots expire after three calendar months; ledger records
and saved transaction FX do not expire.

After the owner deploys with `bash scripts/deploy.sh`, verify in the private bot:

1. Send `/currency EUR` and `/buy GOOGL 5 100 EUR`. This records a real ledger
   entry, so use a purchase you actually made or void the test record afterward.
2. Send `/position GOOGL` and `/portfolio`. Check that stock and FX dates and any
   unavailable or stale values are clear. If required, select `/provider finnhub`
   and enable `/monitor on`; quotes become available as the feed observes them.
3. Record a valid partial `/sell GOOGL 2 125 EUR`, inspect `/trades GOOGL`, and
   void a mistaken transaction by its ID. A buy cannot be voided while later
   non-void sales would oversell; void or correct those sales first.
4. Set `/portfolio_summary HH:MM` to an upcoming local time and confirm one
   message arrives. Set it to `off` if the schedule is not wanted. The installer
   already performs the restart and persistence check.

Deployment and the Telegram walkthrough have to be performed on the actual Pi;
local fixture checks do not substitute for those observations.
