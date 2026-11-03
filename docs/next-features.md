# trabe feature roadmap

Updated 2026-11-03. Position tracking is the next priority. A daily predictor and
advisor follows earnings, insider transactions and news briefings, and starts
with simulated positions so its results can be measured.

Everything stays in private Telegram messages. The Pi uses outbound connections
and the existing rootless Podman deployment; no website or new inbound port.
Commands below are proposals until their release is implemented.

## Current release: 0.3 — deployed

Finnhub intraday monitoring, previous-close dip alerts, per-stock rules,
pause/resume, daily observed-price summaries and three-month price retention are
running on the Pi. Existing watchlists and preferences survive upgrades.
See the [0.3 release contract](release-0.3.md), [verification record](validation.md)
and [Telegram walkthrough](try-0.3.md).

## Release order

| Release | Priority | Useful outcome |
| --- | --- | --- |
| **0.4 — next** | Positions and progress | Record purchases and sales; see position value and profit/loss using the current feed; receive progress messages. |
| 0.5 | Earnings watchdog | Know which held or watched companies report soon and receive a configurable warning. |
| 0.6 | Insider buys | See meaningful executive/director open-market purchases with source links and filing dates. |
| 0.7 | News and morning briefing | Receive a concise digest connecting news, earnings, insider activity and holdings. |
| 0.8 | Daily predictor/advisor with paper trading | Generate timestamped forecasts and suggestions; measure them using simulated positions before considering any use with real holdings. |

Ship these as small usable additions. Each release reuses the current prices,
Telegram controls, durable delivery and storage rather than starting another app.

## 0.4 — positions, purchase prices and progress

The 0.4 implementation adds EUR/USD reporting and historical FX. Its commands and
operational limits are documented in [the 0.4 release notes](release-0.4.md).
The following section retains the original planning context; deployed 0.3 remains
the live release until the owner performs the 0.4 rollout.

Record what was bought, how much, at what price and when. Start with long
positions in supported US-listed USD stocks, including fractional quantities.
These commands record transactions entered by the owner; they do not place
brokerage orders.

### Proposed Telegram commands

| Command | Purpose |
| --- | --- |
| `/buy VST 10 140.50` | Record a purchase of 10 shares at USD 140.50; default date is today. |
| `/buy VST 5 138.00 2026-11-03` | Record another purchase, optionally with an explicit date. |
| `/sell VST 4 150.00` | Record a full or partial sale and its realized result. |
| `/portfolio` | Show holdings, invested cost, observed market value and unrealized profit/loss. |
| `/position VST` | Show quantity, average entry, source price/time, cost basis and position return. |
| `/trades VST` | List recorded transactions with IDs so mistakes can be corrected. |
| `/trade_void ID` | Void a mistaken record and recalculate the position, retaining the correction history. |
| `/portfolio_summary 23:00` or `/portfolio_summary off` | Configure an optional daily progress message in the owner's timezone. |

### First implementation

1. **Transaction ledger.** Persist purchases, sales, quantity, execution price,
   currency, date and optional fees. Support repeated purchases, partial sales and
   corrections. Reject overselling and invalid quantities/prices. Use decimal
   arithmetic for money and quantities; weighted-average cost is the initial
   reporting convention, with rounding only for display.
2. **Position valuation.** Reuse compatible Finnhub observations for the ticker.
   Show quantity, average entry, remaining cost basis, market value, unrealized
   profit/loss in USD and percent, and realized profit/loss for recorded sales.
   For an open position, `value = quantity × price`, `unrealized P/L = value −
   remaining cost basis`, and `return = unrealized P/L / remaining cost basis ×
   100`. Recorded fees feed into the cost/proceeds calculations. Totals initially
   describe recorded holdings in USD, not a complete brokerage account balance.
3. **Telegram progress.** Provide an on-demand portfolio report and an opt-in
   daily summary: invested cost, current value, gains/losses, largest contributors
   and changes since the previous comparable snapshot. Distinguish market moves
   from purchases/sales; deposits or a new purchase must not look like profit.
   Reuse cached prices instead of fetching once per message. Pause suppresses
   automatic progress messages while manual reports remain available.
4. **Price and ownership correctness.** Display source time and fresh/stale/closed
   status. Report missing valuations explicitly and label partial totals. A
   watchlist removal never deletes an owned position or stops its required price
   coverage. Use the union of watched and held symbols for subscription limits;
   report the free-plan capacity before silently losing coverage. Changes to
   quantities or cost basis from splits require an explicit adjustment when the
   feed cannot verify them.
5. **Durable foundation.** Keep transaction records and open-position cost basis
   independently of three-month market-price expiry. Retain compact daily
   portfolio snapshots for three calendar months initially. Preserve the ledger
   through restarts, upgrades and backup restores; provide explicit correction
   or deletion controls. These records become inputs to later exposure/risk
   analysis and the advisor.

Keep the existing previous-close dip rule separate from a position's gain/loss
relative to purchase cost. Entry-price alerts and richer risk rules can follow
the initial portfolio release rather than delaying it.

Done means a recorded purchase is valued against the feed, a second purchase and
partial sale calculate correctly, stale/missing prices are labelled, and the
owner can receive one scheduled progress message with settings preserved after a
service restart. Keep a few focused arithmetic/ledger tests and one Telegram
walkthrough for this feature.

## 0.5 — earnings watchdog

Find upcoming reports for held and watched companies. Verify source access and
date certainty, retain when the information was checked, and send configurable
warnings such as 3–5 days before earnings. Handle date changes and deduplicate
warnings. Include position exposure where holdings exist. Source/plan limitations
must be visible before enabling the feature.

## 0.6 — insider purchases

Track structured filings or a verified feed for meaningful CEO/CFO/director
open-market purchases. Distinguish purchases from grants, exercises and other
transaction types. Save transaction date, filing/publication date, role, size and
source link, and deduplicate amended or repeated records. Connect alerts to held
and watched stocks. Later prediction inputs use the time the filing was actually
available, not an earlier transaction date.

## 0.7 — news and morning briefing

Collect and deduplicate news for held/watched stocks. Produce concise summaries
with source links, publication/receipt times and clearly labelled sentiment.
The optional morning briefing combines news, earnings, insider activity,
positions and recent market observations. Retain compact structured signals that
the predictor can consume. Model/provider selection and any API spending cap are
explicit configuration decisions; the briefing should still work without paid
AI summarization.

## 0.8 — daily predictor/advisor, evaluated with simulated positions

Once positions, earnings, insider purchases and news are available, run one
scheduled daily evaluation using their combined state. Start with a paper
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
entry-price alerts, dividends/FX and richer position risk analysis remain useful
follow-ups. They should support the release order above without blocking the
first usable position tracker. Alpaca, Tiingo and Twelve Data remain documented
in [provider research](market-data-research.md) for future use.

## Small development loop

Use focused unit tests for the behavior being changed and one relevant Telegram
check when a feature reaches the Pi. Documentation changes need no app test run
or deployment. Default CI runs basic checks once on one Python version. Extended
integration/container checks and storage soaks are manual options, not routine
gates. See [CONTRIBUTING.md](../CONTRIBUTING.md#testing-policy) for the policy.
