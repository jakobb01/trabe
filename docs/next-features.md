# trabe feature roadmap

Updated 2026-11-08. Source and verified Pi are **0.8.6**. Premarket/VIX context and
richer news follow wider insider
retention and local session times follow restored estimate visibility, corrected
earnings evidence and insider coverage improvements on top of company news quality, Massive
cross-checks, immutable daily snapshots and a news-refresh performance fix,
including the prior 0.7.2 earnings/message work. An earlier direct Pi diagnostic
confirmed 0.8.1 with successful recent Finnhub requests but missing VST results.
The attempted Alpha Vantage EPS fallback was rejected for inaccurate VST data
and disabled. The owner requires free sources only. The 0.8.3 fallback uses
explicit Yahoo news evidence; see [release details](release-0.8.3.md).
See [the conversation handoff](project-status.md) for exact evidence and next steps.

Everything stays in private Telegram messages. The Pi uses outbound connections
and the existing rootless Podman deployment; no website or new inbound port.
The user revised the order: **earnings timing and fresh command views → insider
coverage → market briefing inputs and daily outlook → broader news predictor/advisor
→ paper portfolio and evaluation**. The owner authorized briefing steps 3 and 4
after completing steps 1 and 2. Both new steps are deployed and verified; later stages remain planned. News quality
and daily snapshots are implemented in 0.8, with a
[detailed design](news-and-snapshots-plan.md) and
[implementation checklist](../todo08-news-snapshots.md) and
[release walkthrough](release-0.8.md). The
[additional news API research](research-news-providers.md) recommends Massive
first for coverage and a second assessment, with Marketaux as an optional numeric
cross-check. Massive access passed for GOOGL/VST; Marketaux remains off and
requires its own access check before enablement. New sources remain explicit
Telegram preferences rather than being switched on by the presence of a key.

## Release order and status

| Release | Status | Useful outcome |
| --- | --- | --- |
| 0.3 | Deployed, retained in later releases | Finnhub intraday monitoring, dip alerts, pause/resume and compact three-month price history. |
| 0.4 | Deployed | Record purchases/sales in EUR or USD; see converted position value and profit/loss; optional daily progress. |
| 0.5 | Included in deployed 0.7.0 | Source-qualified upcoming earnings, configurable warnings, date corrections and exposure. |
| 0.6 | Included in deployed 0.7.0 | Meaningful CEO/CFO/director purchases with filing links and non-derivative verification. |
| 0.7 | Deployed | News extracts, labelled heuristic sentiment and an optional combined morning briefing. |
| 0.7.2 | Included in the 0.8 candidate | Cleaner messages, numeric news bias, earnings expectations, quarterly EPS history and result updates. |
| **0.8.0** | Deployed, confirmed in the saved pre-fix diagnostic | Company news quality, optional Massive/Marketaux cross-checks and immutable daily input snapshots. |
| **0.8.1** | Pi version directly confirmed; user confirmed news works | Faster news processing, per-ticker job progress and visible source error details. |
| **0.8.2** | Deployed; Alpha EPS comparison withdrawn | Refresh by default, wider insider verification and coverage reporting. |
| **0.8.3** | Deployed; live command delivery and source hashes verified | Explicit current-quarter news EPS, with basis/source and no unverified beat/miss grade. |
| **0.8.4** | Deployed; live report delivery, briefing card and source hashes verified | Show saved calendar forecasts beside Yahoo EPS in reports, briefing cards and future alerts. |
| **0.8.5** | Deployed; insider refresh, local-time briefing delivery and source hashes verified | Retain 500 primary insider rows, preserve source coverage and show local session times with session-based briefing claims. |
| **0.8.6** | Deployed; live refresh, corrected cached presentation/delivery and module hashes verified | Indicative stock/SPY/QQQ premarket prices, delayed VIX, shared article extracts and broad-market briefing news. |
| **Insider coverage** | Concrete 0.8.2 fixes implemented | Keep transactions beyond row 100, bound date-range splits and expose source/qualification gaps. |
| **Then: market briefing inputs** | Local market clock implemented; data/enrichment planned | Verified free premarket prices, VIX and richer market news. |
| **Then: daily market outlook** | Requested; planned first advisor delivery | A short expectation for today's session grounded in news, VIX and premarket observations. |
| **Then: broader news predictor/advisor** | Planned | Timestamped company predictions and suggestions using available inputs. |
| **Then: paper portfolio/evaluation** | Planned | Separate simulated cash/positions and measured outcomes against benchmarks. |

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

`/earnings` requests a background refresh; `/earnings cached` reads saved data.
Warnings default to five calendar days (configurable 1–14), include available
position exposure and are durably deduplicated. Date changes generate separate
corrections. Sources/limitations: [earnings research](research-0.5-earnings.md).

The **0.7.2** follow-up adds expected EPS/revenue, four recent EPS actual/estimate
comparisons and separate HIT/MET/MISS updates as results arrive. Known pending
releases get five-minute checks around the release window, then hourly checks
through day seven (during daytime New York hours), using existing budgets
and durable delivery. Historical revenue and live earnings-call coverage are not
supplied by this free EPS-history endpoint. See [the 0.7.2 contract](release-0.7.2.md).

## 0.6 — insider purchases

Implemented with Tracefour filings and Finnhub cross-checks. Qualified purchases
require CEO/CFO/director roles, positive meaningful size and non-derivative
verification; known grants/exercises, withholding and offering flags are excluded.
Unknown eligibility remains visible without becoming an alert. SEC code P is
labelled open-market/private when exchange execution is unproven.

`/insiders` requests a background refresh; `/insiders cached` reads saved data.
Automation controls and a configurable USD minimum are available.
Transaction and public filing times, source attribution, amendments and stable
claims are retained. Activation/resume does not replay historical filings.
The 0.8.2 adapter removes local 100-row truncation and splits crowded Finnhub
ranges within a bounded request budget. Coverage reports distinguish healthy
empty purchase feeds, qualification gaps and source errors. Current VST/GOOGL
checks do not require a new insider API. Direct SEC enrichment remains optional future work. Details:
[insider research](research-0.6-insiders.md).

## 0.7 — news and morning briefing

Implemented with Finnhub company-news and explicit Yahoo RSS fallback, canonical
URL/ID deduplication, source links, publication/receipt times and compact daily
signals. Summaries are two-sentence extracts from supplied snippets/headlines;
sentiment is a labelled local heuristic. Paid or local model summarization is an
optional later addition, not a prerequisite.

The 0.7.1 presentation update makes Telegram news concise: bullish/neutral/bearish
with signed bias (−1.0 to +1.0), a short headline and its age. Source links, extracts and model/provider
metadata stay in storage rather than the default messages. Bold headings/tickers
and italic notes apply across Telegram reports. A later **custom LLM/classifier**
can digest these saved inputs and replace the current heuristic; its model,
evaluation and cost decisions are deferred. See [message examples](messages.md).

`/news` requests a background refresh; `/news cached` reads saved data. The same
commands accept a ticker after `cached` or `refresh`. Provider budgets and
cooldowns still apply; unavailable sources leave cached or partial results.
`/briefing now`
refreshes and combines news, earnings, insider activity, positions and cached
market observations. An opt-in schedule runs once per configured local US trading
day before market open. Missing/stale sections stay visible. All four new
automations default off, share existing provider budgets, respect pause and keep
HTTP work out of Telegram command handling. Details:
[news research](research-0.7-news.md) and [release notes](release-0.7.md).

## 0.8 — news quality and daily snapshots

Implemented in the accepted order above. News quality introduces
target-specific relevance, conservative story grouping, improved rules, optional
additional feeds/vendor sentiment and an optional model interface. Keep a consistent
local company bias and report vendor agreement/disagreement on matched stories;
do not average unrelated article baskets or count syndicated copies twice. Daily
snapshots then freeze the actual available input set at a declared decision time.
Paper positions and forecasts follow these foundations. See the
[code plan](news-and-snapshots-plan.md) for files, schema
migrations, bounded storage, Telegram controls and focused acceptance checks.
See [source research and free-tier budgets](research-news-providers.md) for the
Massive-first adapter plan, optional Marketaux scores and deferred alternatives.
New keys are optional for the local-rules stage; no heavyweight Pi model is needed.

The candidate has a rules analyser contract rather than an installed LLM.
Source/model calibration and realistic evaluation remain open research. The
60 synthetic acceptance examples protect declared behavior; they do not establish
sentiment accuracy on live news or predictive trading performance.

## Next — insider coverage, market briefing and daily outlook

The owner requested premarket prices, market-open time in their current timezone,
VIX and a few words predicting what to expect in today's market. The proposed
order delivers useful Telegram improvements before combining them into an
outlook. Track the work in [the market briefing checklist](../todo-market-briefing.md).

| Order | Delivery | Why here |
| --- | --- | --- |
| 1 | Insider coverage checkpoint: complete, deployed in 0.8.5. | [Review](insider-coverage-review.md) confirms healthy current-symbol coverage; the remaining primary-row/snapshot limits and independent source freshness are fixed. No new API needed. |
| 2 | Local market-open time: complete, deployed in 0.8.5. | Show open/close/countdown in the saved timezone and claim briefings per exchange session across travel/local midnight. |
| 3 | Premarket/VIX: complete, deployed in 0.8.6. | Verified Yahoo chart access, SPY/QQQ selected by owner, explicit session/baseline/delay gaps and shared budgets. No new key/paid API. |
| 4 | News evidence: complete, deployed in 0.8.6. | Shared bounded article cache, selected Yahoo RSS alongside Finnhub, source-qualified/dated extracts and a separate broad-market news view. |
| 5 | Add a short daily market outlook. | Combine the verified inputs into an opening expectation, main driver and risk; save it before the outcome. This is the first advisor feature. |
| 6 | Expand the company advisor, then add paper portfolio/evaluation. | Reuse the recorded inputs and outlooks; keep the requested advisor-before-paper order. |

Use the existing IANA timezone setting, changed through Telegram when the owner
travels. Telegram does not automatically supply a travelling user's timezone.
Convert the actual exchange session open rather than hardcoding a local hour;
cover DST mismatch weeks, holidays, early closes and timezone changes. Keep the
briefing schedule consistent with the exchange session even when the owner's
local date differs from New York's. In 0.8.5 the market-status line uses local
session times, with the next verified open on holidays/weekends. HH:MM schedules
apply to the local date of the session open; duplicate claims use its US date.

Premarket prices should identify the symbol, price/currency, change from the
previous regular-session close, source and observation time. Cover held/watched
symbols and a small agreed set of broad-market references. Prior-close, delayed,
stale and missing observations must remain distinguishable from fresh premarket
trades. Add VIX level, change and observation time as volatility context; VIX
alone does not establish price direction. Prefer existing providers, but verify
their free entitlements and actual fields first. If another free API/key is
necessary, report the specific gap and proposed source before enabling it.

News currently has Finnhub, Yahoo RSS fallback and Massive adapters. The Yahoo
full-article reader is currently specific to earnings; shared article enrichment
and broad-market news coverage remain work to do. Preserve publication/receipt
times, deduplication and request budgets, and distinguish article-backed extracts
from headline-only evidence. Marketaux remains optional/unverified.

The initial outlook should be one or two short sentences about **today's US
session**, with a directional or mixed opening expectation, volatility context
and the main risk/condition that could change it. Separate observed facts from
inference. Select its benchmark and evaluation horizon before implementation;
do not turn the existing news-bias score or VIX into invented probability/confidence.
Sparse, conflicting or stale inputs should produce a qualified or unavailable
outlook. Store the issued text, source observations, as-of time and rule/model
version in a bounded record, with the exact inputs available when issued.

## Later — broader news predictor/advisor and paper evaluation

Build the broader news predictor/advisor before the paper portfolio, as requested.
When choosing the predictor's inputs, add the exact historical-price window it
consumes, market-context fields and evidence to new snapshot versions. Existing
v1 snapshots preserve chosen prices and baselines, not an entire price-history
archive, and remain unchanged.

The 0.8 foundation provides frozen positions, earnings, insider purchases and news as
inputs. Extend this with verified market context and timestamped outlooks, then
record company forecasts before introducing paper execution. Recommendations
remain messages for review.

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

The initial advisor can produce research suggestions; assess its performance
with the later paper portfolio before using it to guide real holdings. Automated
brokerage execution is outside this roadmap.

## Additional backlog

Shared article caching and richer briefing evidence are delivered in 0.8.6.
Support for more public publishers and evaluation of live relevance/extract
quality remain useful follow-ups before relying on an advisor's conclusions.

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
