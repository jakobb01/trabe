# trabe feature roadmap

Updated 2026-11-04. The Pi is deployed on 0.4.0. Milestones 0.5–0.7 are
implemented together as a **0.7.0 candidate ready to deploy**, with research and
local verification complete. Deployment and its Telegram walkthrough are the
next operational step; no 0.7 deployment is claimed here.

Everything stays in private Telegram messages. The Pi uses outbound connections
and the existing rootless Podman deployment; no website or new inbound port.
The next feature milestone is the advisor evaluated with simulated positions.

## Release order and status

| Release | Status | Useful outcome |
| --- | --- | --- |
| 0.3 | Deployed, retained in later releases | Finnhub intraday monitoring, dip alerts, pause/resume and compact three-month price history. |
| 0.4 | Deployed | Record purchases/sales in EUR or USD; see converted position value and profit/loss; optional daily progress. |
| 0.5 | Implemented in the 0.7 candidate | Source-qualified upcoming earnings, configurable warnings, date corrections and exposure. |
| 0.6 | Implemented in the 0.7 candidate | Meaningful CEO/CFO/director purchases with filing links and non-derivative verification. |
| 0.7 | Ready to deploy | Source-linked news extracts, labelled heuristic sentiment and an optional combined morning briefing. |
| **0.8 — next** | Planned | Timestamped daily predictions/suggestions, evaluated with a separate paper portfolio before considering real holdings. |

See [0.7 release notes and walkthrough](release-0.7.md), the
[completed implementation checklist](../todo05-07.md) and
[verification record](validation.md#07-candidate-2026-11-04).

## 0.4 — positions, purchase prices and progress

The durable transaction ledger supports fractional quantities, repeated buys,
partial sales, fees and auditable void/quantity/basis corrections. Decimal money
arithmetic and weighted-average cost drive realized/unrealized results. Trades
are records entered by the owner, not brokerage orders.

`/currency EUR` sets input and reporting defaults. For example,
`/buy GOOGL 5 100 EUR` records five shares at EUR 100 per share. Compatible USD
market prices are converted with current Frankfurter/ECB reference FX; original
transactions and execution-date FX provenance remain saved. Pending FX,
missing/stale prices and partial totals are explicit. A watchlist removal does
not delete a holding or its price coverage.

`/portfolio`, `/position`, `/trades`, `/sell`, `/trade_void`, `/position_adjust`,
`/basis_adjust` and `/portfolio_summary` are implemented. Daily progress excludes
holdings whose trade history changed, so new purchases/sales do not look like
market gains. Ledger and historical transaction FX do not expire with market
history. Details: [0.4 release contract](release-0.4.md).

## 0.5 — earnings watchdog

Implemented for held and watched symbols. Finnhub calendars are queried per
symbol; Alpha Vantage's cached bulk CSV is an explicit fallback. Provider-reported
dates, fiscal periods, time categories and checked times remain visible. Source
conflicts are distinguished from verified date revisions.

`/earnings`, `on`, `off` and `refresh` provide cached views and background work.
Warnings default to five calendar days (configurable 1–14), include available
position exposure and are durably deduplicated. Date changes generate separate
corrections. Sources/limitations: [earnings research](research-0.5-earnings.md).

## 0.6 — insider purchases

Implemented with Tracefour filings and Finnhub cross-checks. Qualified purchases
require CEO/CFO/director roles, positive meaningful size and non-derivative
verification; known grants/exercises, withholding and offering flags are excluded.
Unknown eligibility remains visible without becoming an alert. SEC code P is
labelled open-market/private when exchange execution is unproven.

`/insiders`, `on`, `off`, `refresh` and a configurable USD minimum are available.
Transaction and public filing times, source attribution, amendments and stable
claims are retained. Activation/resume does not replay historical filings.
Direct SEC enrichment remains optional future work. Details:
[insider research](research-0.6-insiders.md).

## 0.7 — news and morning briefing

Implemented with Finnhub company-news and explicit Yahoo RSS fallback, canonical
URL/ID deduplication, source links, publication/receipt times and compact daily
signals. Summaries are two-sentence extracts from supplied snippets/headlines;
sentiment is a labelled local heuristic. Paid or local model summarization is an
optional later addition, not a prerequisite.

`/news` provides cached reports and background refresh controls. `/briefing now`
refreshes and combines news, earnings, insider activity, positions and cached
market observations. An opt-in schedule runs once per configured local US trading
day before market open. Missing/stale sections stay visible. All four new
automations default off, share existing provider budgets, respect pause and keep
HTTP work out of Telegram command handling. Details:
[news research](research-0.7-news.md) and [release notes](release-0.7.md).

## 0.8 — daily predictor/advisor, evaluated with simulated positions

The 0.7 candidate provides positions, earnings, insider purchases and news as
inputs. The next milestone is one scheduled daily evaluation using their combined state. Start with a paper
portfolio and measure the outcome; recommendations remain messages for review.

1. **Freeze the daily inputs.** Store an as-of timestamp and compact snapshot of
   available news/sentiment, upcoming earnings, published insider activity,
   price history, positions, exposure and data-quality gaps. Preserve enough
   provenance to explain a decision later, even after ordinary market-history
   expiry. Define a bounded retention/export policy for experiment records.
2. **Record a forecast before its outcome.** Select a fixed forecast horizon
   before the experiment, such as the next five trading sessions. Produce a
   direction/range, confidence estimate, reasoning with sources, risk flags and a
   proposed hold/add/reduce/avoid action. Save the model/version, inputs and
   configuration. Missing or stale inputs can produce an explicit abstention;
   confidence is measured against outcomes before being treated as reliable.
3. **Paper portfolio.** Keep simulated cash, positions and trades separate from
   the owner's actual transaction ledger. Configure starting cash, allocation
   limits, fees and slippage through Telegram. Apply fills at a qualifying price
   observed after the decision, with fixed execution rules and exchange-session
   handling. Never use future news or a price from before the forecast to grant
   the simulator an impossible fill.
4. **Evaluate daily.** Compare net returns, drawdown, exposure, turnover and costs
   with fixed buy-and-hold/cash benchmarks under comparable assumptions. Track
   forecast accuracy at the declared horizon, abstentions, missing data and
   confidence calibration where there are enough observations. Send a concise
   progress message linking the forecast, simulated action and measured outcome.
5. **Make experiments comparable.** Version the strategy and keep predictions
   and outcomes unchanged after the fact. Keep a distinct forward paper run when
   tuning a strategy on historical data. Report the sample size and evaluation
   period; a short profitable run alone does not establish predictive skill.

Paper trading is the first delivery target. Any later use as an advisor for real
holdings is a separate milestone after reviewing forward results. Automated
brokerage execution is outside this roadmap.

## Additional backlog

Weekly/history comparisons, threshold ladders, recovery/rearming rules,
entry-price alerts, dividends, more currencies and richer position risk analysis remain useful
follow-ups. They should support the release order above without blocking the
first usable position tracker. Alpaca, Tiingo and Twelve Data remain documented
in [provider research](market-data-research.md) for future use.

## Small development loop

Use focused unit tests for the behavior being changed and one relevant Telegram
check when a feature reaches the Pi. Documentation changes need no app test run
or deployment. Default CI runs basic checks once on one Python version. Extended
integration/container checks and storage soaks are manual options, not routine
gates. See [CONTRIBUTING.md](../CONTRIBUTING.md#testing-policy) for the policy.
