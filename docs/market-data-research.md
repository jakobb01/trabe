# Free intraday market data for trabe

Research date: 2026-11-03. Initial scope: a private US-stock watchlist such as
GOOGL and VST. Findings below come from provider documentation and pricing, not
authenticated tests with newly created accounts. Actual symbol entitlements and
timestamp freshness still need to be checked with the selected free key.

## Selected approach for 0.3

The user selected **Finnhub WebSocket for 0.3**, with an account-validation gate.
Its free personal plan lists 60 REST requests/minute and 50 WebSocket symbols.
That is substantially more useful for this watchlist than the current end-of-day
source. [Finnhub pricing](https://finnhub.io/pricing)

Use its stream for continuous monitoring: the quote documentation recommends
WebSockets over constant polling. REST remains useful for initial quotes,
previous-close baselines and bounded recovery. Historical candles are marked
premium, so collect our own observations from activation onward; do not promise
free historical candle backfill. [Quote endpoint](https://finnhub.io/docs/api/quote),
[candles endpoint](https://finnhub.io/docs/api/stock-candles)

Its streaming documentation permits one connection per API key and timestamps
trades in milliseconds. Use one shared connection and normalize timestamps before
validation. [Streaming documentation](https://finnhub.io/docs/api/websocket-trades)

**Alpaca Basic is the strongest documented alternative** for future consideration,
especially for batched REST polling. Tiingo and Twelve Data remain documented
below. Only the Finnhub intraday adapter is in scope for 0.3.

## Candidates

| Provider | Published free allowance | Useful feed | Main constraint |
| --- | --- | --- | --- |
| Finnhub | 60 REST calls/minute; 50 streaming symbols | Real-time US stock quotes/trades | Personal use; verify source/feed characteristics and actual symbol access |
| Alpaca Basic | 200 historical API calls/minute; 30 streaming symbols | Real-time IEX; multi-symbol REST requests | A subset of US trading venues; needs a key/secret pair |
| Tiingo Starter | 50 requests/hour, 1,000/day, 500 unique symbols/month | Derived real-time reference pricing | Derived price semantics and the hourly ceiling |
| Twelve Data Basic | 8 API credits/minute, 800/day; trial streaming allowance | Real-time US equities | Credits are per symbol; free plan's display permissions need clarification |

### Finnhub

The quote API documents real-time US stocks and supplies current price,
previous close and a source timestamp. International real-time access is described
as an enterprise offering. The inspected material does not establish that the
free feed is a consolidated all-venue tape; do not label it that way.
[API documentation](https://finnhub.io/docs/api/quote)

The pricing page is rendered with JavaScript. Its official frontend bundle was
inspected to verify the free quota and streaming-symbol row. The documentation's
embedded OpenAPI schema was also inspected directly. These are primary-source
checks, not a claim that a real free-account connection has been tested.

### Alpaca

Basic is free for paper/live accounts and provides real-time IEX equity data.
Its all-exchange SIP feed has different entitlements. The published plan lists
200 historical API calls/minute and 30 streaming symbols.
[Plan details](https://docs.alpaca.markets/us/docs/about-market-data-api)

REST latest-trade and snapshot endpoints accept lists of symbols; specify
`feed=iex` explicitly. A snapshot includes the latest trade/quote and current and
previous daily bars, allowing a consistent baseline from the chosen feed.
[Latest trades](https://docs.alpaca.markets/us/reference/stocklatesttrades-1),
[snapshots](https://docs.alpaca.markets/us/reference/stocksnapshots-1)

A paper-only account can be created globally and is entitled to IEX data. This
option does not require trabe to place trades or maintain a funded trading account.
[Paper-account documentation](https://docs.alpaca.markets/us/docs/paper-trading)

IEX is one venue, so quotes can differ from an all-market screen and individual
symbols can have gaps between observed trades. Feed coverage and quote age need
to be visible in trabe. [Provider FAQ](https://docs.alpaca.markets/us/docs/market-data-faq)

### Tiingo

Starter publishes 50 requests/hour, 1,000/day and 500 unique symbols/month.
[Pricing](https://www.tiingo.com/about/pricing)

The IEX endpoint offers `tngoLast`, a derived reference price. Raw last-trade and
bid/ask fields require IEX entitlements under the policy described in its docs;
do not assume `last` will be populated for a new free account. Batch ticker
requests are supported. [IEX documentation](https://www.tiingo.com/documentation/iex),
[batch support](https://www.tiingo.com/documentation/general/changelog)

There is also a newer free consolidated-derived Equity Realtime API. It is marked
beta, and the provider recommends its IEX endpoints for production use. Keep this
as an experiment until actual free-key access and field freshness are established.
[Equity Realtime documentation](https://www.tiingo.com/documentation/equity-realtime-stock-data)

### Twelve Data

Basic lists 8 credits/minute and 800/day; multi-symbol calls still consume credits
per symbol. Its 8 WebSocket credits are labelled as trial access, so they are not
a dependable general-purpose free streaming entitlement.
[Pricing and credit examples](https://twelvedata.com/pricing)

The default US feed covers listed stocks but represents about 5% of US trading
volume. It is not equivalent to a consolidated tape.
[Feed coverage](https://support.twelvedata.com/en/articles/9935903-us-equities-market-data)

The current Basic description says internal non-display use. Because trabe shows
prices in a private Telegram chat, confirm that use with the provider before
choosing it. This makes it a conditional candidate even when the technical quota
fits. [Plan usage description](https://twelvedata.com/pricing)

## Other services checked

| Service | Why it is not the first choice for free continuous monitoring |
| --- | --- |
| [Massive, formerly Polygon](https://massive.com/pricing?product=stocks) | Free stocks plan is end-of-day, 5 calls/minute. Historical minute aggregates do not make the free tier live. |
| [Financial Modeling Prep](https://site.financialmodelingprep.com/developer/docs/pricing) | Basic offers 250 calls/day and advertises end-of-day/reference data; its free quota does not establish live access for our tickers. |
| [Marketstack](https://marketstack.com/pricing) | Free is end-of-day with 100 requests/month; intraday access is paid. |
| [EODHD](https://eodhd.com/pricing) | Free lists 20 calls/day and delayed live data; that quota is too small for frequent watchlist checks. |
| [Yahoo through yfinance](https://github.com/ranaroussi/yfinance) | The library is independent of Yahoo and intended for research/personal use. Prefer a provider with explicit account entitlements and quotas for the first production integration. |

## Capacity for this app

The following are **our calculations**, assuming a normal 390-minute US core
session. Holidays and early closes reduce the session length.
[NYSE hours/calendar](https://www.nyse.com/trade/hours-calendars)

| Watchlist | One REST quote per symbol each minute | Minute samples per full session |
| --- | --- | --- |
| 2 symbols | 2 calls/minute; 780/day | Up to 780 |
| 10 symbols | 10 calls/minute; 3,900/day | Up to 3,900 |
| 20 symbols | 20 calls/minute; 7,800/day | Up to 7,800 |

This arithmetic fits Finnhub's published minute quota, but the provider recommends
streaming for ongoing updates. With a stream, subscription capacity matters more
than REST polling volume. Actual samples can be fewer when a symbol has no new
source observation; repeated timestamps are not additional market data.

For Twelve Data, two symbols every minute would spend 780 of 800 daily credits
before diagnostics or manual requests. A five-minute interval for those two
symbols would spend 156. Ten symbols every ten minutes would spend 390 credits;
requests must also be spread within the minute limit.

With an Alpaca batch that contains the whole small watchlist, a one-minute check
would use roughly 390 HTTP requests/session. A Tiingo batch every two minutes
would use roughly 195/day and 30/hour. These are endpoint-based estimates to verify
against account counters, not measured usage.

## Consequences for release 0.3

1. Bring the provider boundary, intraday ingestion and timestamped price storage
   into 0.3. Keep Alpha Vantage as an explicitly labelled end-of-day option.
2. Prefer one outbound Finnhub WebSocket with bounded REST bootstrap/recovery.
   It needs no inbound port. Use a small WebSocket dependency rather than writing
   the protocol by hand; validate its ARMv7 installation in the production image.
3. Store source UTC time, receipt time, session date, currency, provider/feed and
   baseline provenance. Keep minute detail during the session, then roll it up
   to daily summaries retained for three calendar months. See the measured
   [storage and retention plan](storage-and-retention.md). Partial-feed samples
   must not be described as complete consolidated OHLCV bars.
4. Calculate intraday change against the validated previous session close from
   the selected provider/basis. Handle splits and missing baselines; do not combine
   a fresh intraday price with an unrelated cached daily percentage change.
5. Schedule by the exchange session, including DST, holidays and early closes.
   Keep monitoring opt-in and configurable in Telegram.
6. Alert once per symbol/rule/session on the first eligible breach. Reject stale,
   future, duplicate and out-of-session observations; show feed outages and gaps.
7. Validate GOOGL/VST with a real free account during an open session before
   enabling alerts. Test reconnects, quota denial and expired timestamps.

The research establishes a credible free path for US intraday monitoring. It does
not establish free global real-time coverage or a guaranteed full-market feed.
The selected provider key is the remaining external prerequisite for the account
validation step; enter it locally, outside Telegram and Git.
