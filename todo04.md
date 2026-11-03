# trabe 0.4 — implementation checklist

Implement position tracking and Telegram progress reports on top of deployed 0.3.
Read `AGENTS.md` and `docs/next-features.md` first. This checklist adds default
currency and EUR/USD conversion to 0.4, bringing FX forward from the later backlog.
Keep the implementation small; earnings, insider feeds, news and the predictor
remain later releases.

## 1. Currency settings and transaction input

- [x] Add a persistent `default_currency` preference, initially `EUR`; support
  `EUR` and `USD` first. Configure it through `/currency EUR` or `/currency USD`;
  `/currency` shows the current setting.
- [x] Use the default currency for transaction input when currency is omitted,
  and for portfolio values, profit/loss and progress messages.
- [x] Allow an explicit transaction currency that overrides the input default.
  Save that currency with the transaction permanently; changing the preference
  must never reinterpret an earlier purchase price.
- [x] Implement these Telegram commands and add them to help/menu:

  | Command | Meaning |
  | --- | --- |
  | `/buy GOOGL 5 100 EUR` | Record five shares bought at EUR 100 per share. |
  | `/buy GOOGL 5 100` | Same purchase when the default currency is EUR. |
  | `/buy GOOGL 5 110 USD 2026-11-03` | Record a purchase with explicit currency and date. |
  | `/sell GOOGL 2 125 EUR` | Record a partial sale, priced per share. |
  | `/portfolio` | Show holdings and totals in the default currency. |
  | `/position GOOGL` | Show entry cost, quantity, market value and profit/loss. |
  | `/trades GOOGL` | List transactions and their IDs. |
  | `/trade_void ID` | Correct a mistaken entry by voiding it and recalculating. |
  | `/portfolio_summary 23:00` or `/portfolio_summary off` | Configure daily progress messages. |

- [x] Define input as `TICKER QUANTITY PRICE [CURRENCY] [YYYY-MM-DD]`; default
  date to today in the owner's timezone. Show clear examples for invalid input.
- [x] Support fractional shares, positive prices/quantities and optional
  nonnegative fees in the transaction's currency. Use decimal arithmetic and
  round for display only.
- [x] Confirm records with transaction ID, quantity, per-share price, total,
  currency and date. These commands record trades the owner already made.

## 2. Durable transaction ledger and cost basis

- [x] Store original transaction amounts, currency, date, fees, correction state
  and timestamps in SQLite. Preserve existing 0.3 settings, watchlist and data.
- [x] Support multiple purchases and weighted-average remaining cost basis,
  partial/full sales, realized profit/loss and corrections. Reject overselling.
- [x] Preserve the original ledger when deriving positions; voiding an entry must
  retain its audit record and reject an inconsistent resulting sale history.
- [x] Ensure replaying the same Telegram update cannot record the trade twice.
- [x] Keep transactions and open-position cost basis outside three-month
  market-history expiry. Keep compact portfolio snapshots for three months.
- [x] Handle supported US stocks purchased in EUR or USD. Additional exchanges,
  short positions, dividends and tax reporting can follow later.

## 3. FX data and conversion

- [x] Select a free FX source during implementation. Verify EUR/USD access,
  historical rates and update frequency before adding the adapter. Reuse a
  current provider if its free entitlement covers the requirements.
- [x] Cache and share FX requests; do not fetch an exchange rate on every trade
  tick or Telegram message. A daily reference rate is sufficient initially.
- [x] Store explicit conversion direction, provider, effective rate date and
  retrieval time. For example, `1 USD = 0.90 EUR` means multiply USD amounts by
  `0.90` to obtain EUR. Handle the inverse consistently.
- [x] Convert Finnhub's USD market price into the default currency before
  displaying position value or comparing it with purchase cost.
- [x] Preserve actual EUR purchase cost as EUR cost: a later FX movement must
  not rewrite the amount the owner originally paid.
- [x] For a transaction in another currency, normalize its cost/proceeds using
  its execution-date FX rate. Preserve that historical rate and provenance;
  never substitute today's rate for a backdated transaction silently.
- [x] Support cost-basis reporting in both supported currencies using the saved
  transaction amounts and historical FX rates. A change of default currency
  changes presentation and new input defaults, not the original ledger.
- [x] For weekends/holidays, use the latest published rate on or before the
  relevant date and disclose its date. Define a freshness policy appropriate to
  the FX source's publication calendar.
- [x] Permit saving original trade records during an FX outage. Mark conversions
  that need unavailable historical/current rates as pending or unavailable;
  never invent a rate or silently use 1:1.
- [x] Display stock-price and FX dates separately. Label daily reference FX as
  such; its conversion can differ from a broker's execution rate/spread.

## 4. Position valuation and profit/loss

- [x] Reuse Finnhub observations and the existing freshness/session handling.
  No new price feed is needed for this release.
- [x] Calculate in the selected reporting currency:
  - Market value = remaining shares × converted current per-share price.
  - Unrealized profit/loss = market value − remaining historical cost basis.
  - Unrealized return = unrealized profit/loss ÷ remaining cost basis × 100.
  - Realized profit/loss = net recorded sale proceeds − allocated cost basis.
- [x] Include recorded fees consistently. Explain that returns in the reporting
  currency include FX movement as well as the stock's price movement.
- [x] Show quantity, average entry, cost basis, current price, market value,
  unrealized amount/percent and realized result where applicable.
- [x] Show the original USD quote alongside its conversion when useful. Clearly
  label stale, closed-market, missing-price and missing-FX valuations.
- [x] Sum only compatible converted amounts; identify incomplete portfolio
  totals and exclude unavailable values explicitly.
- [x] Monitor the union of held and watched symbols, respecting the free
  subscription cap. Removing a watchlist item must not delete its position or
  stop required price coverage without explanation.
- [x] Keep existing previous-close dip alerts distinct from entry-cost returns.
  Flag suspect corporate-action mismatches and support explicit position
  adjustment when quantities/basis need correction; do not guess split factors.

### Required example

- [x] With default currency EUR, `/buy GOOGL 5 100 EUR` records EUR 500 cost.
  Given a fixture quote of USD 120 and `1 USD = 0.90 EUR`, show EUR 108 per
  share, EUR 540 market value, EUR +40 unrealized profit and +8%, before fees.
  These are illustrative inputs, not current market prices.

## 5. Telegram progress messages

- [x] Make `/portfolio` and `/position` usable on demand from cached data.
- [x] Add opt-in daily summaries in the owner's timezone: invested cost,
  observed value, profit/loss, largest contributors and progress since the
  previous comparable snapshot, all in the default currency.
- [x] Separate market performance from purchases/sales; added capital must not
  appear as profit. Handle a reporting-currency change without a false jump in
  progress or a comparison between unlike snapshots.
- [x] Respect `/pause` and `/resume`, durable outbox delivery and per-day summary
  deduplication. Manual reports must remain usable while automation is paused.

## 6. Finish with a small validation loop

- [x] Add only focused unit coverage for ledger arithmetic/corrections, partial
  sales, the EUR example, historical FX/default-currency changes and missing data.
- [x] Run relevant tests and lint once after implementation. Reuse passing
  results until the affected code changes or a failure needs investigation.
- [x] Skip broad integration reruns, ARM emulation and storage soaks unless a
  specific failure makes one necessary. Do not add a new test framework.
- [x] Update help, README, release notes and version to 0.4.0 when implemented.
- [ ] Deploy once through the existing rootless Podman/SSH workflow. Preserve
  credentials, existing state and the no-new-inbound-ports setup.
  Review status (2026-11-03): 47 focused tests and the changed-file checks pass.
  SSH connectivity returned after the owner rebooted the Pi; deployment is in
  progress through the normal script.
- [ ] Verify one Telegram walkthrough: select EUR, record a purchase, view
  position/portfolio, record a partial sale, correct an entry and receive a
  scheduled progress message. Verify persistence through the deployment's
  existing restart check; avoid duplicate manual restart testing.

## Handoff result

- [ ] Report the implemented commands, supported currencies, FX source/update
  frequency, relevant checks and any specific unfinished item. Keep real
  positions usable as clean inputs for the later risk analysis and paper advisor.
