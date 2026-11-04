# 0.7 research: news, sentiment and combined morning briefing

Researched 2026-11-03/04. This is the implementation recommendation, based on
official client contracts and read-only live requests. No new account, paid AI
key, Telegram poller or Pi change is required.

## Selected sources and actual entitlement

| Source | Observed result | Decision |
| --- | --- | --- |
| Finnhub `GET /company-news?symbol=…&from=…&to=…` with the existing key in `X-Finnhub-Token` | HTTP 200 JSON arrays. Seven-day requests returned 249 GOOGL items and 39 VST items; every item had a URL, and all but one had a summary. | Primary news source. |
| Finnhub `GET /news-sentiment?symbol=GOOGL` | HTTP 403 with the same key. | Do not depend on this endpoint or imply free premium sentiment access. |
| Yahoo ticker RSS at `https://feeds.finance.yahoo.com/rss/2.0/headline?s=GOOGL&region=US&lang=en-US` | HTTP 200 RSS, 20 items. | Explicitly labelled best-effort fallback; it is not a contracted market-data API. |

The private evidence is `.local/validation/news-api-probe.json`. It records status,
shape, counts and field names, never credentials or token-bearing URLs. The news
records contain `category`, `datetime`, `headline`, `id`, `image`, `related`,
`source`, `summary`, and `url`. The probe did not claim every item was relevant or
that the feed covered all publishers.

Primary references:

- [Finnhub company-news documentation](https://finnhub.io/docs/api/company-news).
- [Finnhub's official Python client](https://github.com/Finnhub-Stock-API/finnhub-python),
  which documents `company_news(symbol, _from, to)` and `news_sentiment(symbol)`.
- [Official client implementation](https://github.com/Finnhub-Stock-API/finnhub-python/blob/master/finnhub/client.py),
  showing the endpoint names and date parameters. Reuse our header-based secret
  handling rather than its query-token convention.
- [Yahoo's live ticker RSS endpoint](https://feeds.finance.yahoo.com/rss/2.0/headline?s=GOOGL&region=US&lang=en-US).
- [SEC newsroom](https://www.sec.gov/newsroom/press-releases) advertises an official
  RSS feed, but it covers SEC actions rather than company-specific financial
  news; it is an optional later source, not a replacement for the selected feed.

Issuer investor-relations feeds can later improve direct-source coverage. They
need per-company discovery and are not necessary to finish 0.7. Reuters website
scraping and full article crawling are also unnecessary for this release.

## Useful summaries without another key

Use the provider's supplied summary and headline. Strip markup, normalize
whitespace, deduplicate repeated headline text and select at most two concise
sentences. Keep a source link and publication time. If the summary is absent,
use the headline and a factual source/date sentence, explicitly marking the item
as headline-only. Never invent missing article content.

Add a small deterministic financial lexicon that considers phrases such as
earnings beats/misses, guidance raised/cut, approval/rejection, and simple nearby
negation. Return `Bullish`, `Bearish`, or `Neutral`, with a bounded numeric score
and method `lexicon-v1`. Label it **heuristic sentiment**, derived from the
available headline/summary, rather than a price forecast or a paid vendor score.
No matched evidence should produce Neutral rather than an invented judgment.

Expose a pure helper such as `summarize(headline, summary) -> {summary,
summary_kind, sentiment, score, method}`. This provides immediate useful digests
and versioned structured inputs for the future advisor.

A later optional local Ollama adapter can use standard-library HTTP with a
locally configured endpoint/model, timeout, bounded input/output and a strict
response schema. It should be disabled by default and fall back to the
extractive/heuristic path. Installing a model or requiring a new AI key must not
block 0.7.

## Collection, identity and storage

- Cover `Ledger.coverage()`: the union of held and watched symbols. Prioritize
  held symbols in output, without hiding watched-symbol errors.
- Fetch an initial seven-day window, then overlap the previous successful fetch
  by one or two days to catch delayed items. Store one item per source ID and/or
  canonical article URL; associate it with multiple tickers through a join table.
- Preserve `published_at` from the provider, `first_seen_at`, `last_seen_at`,
  provider and publisher, original URL, canonical URL and the requested/related
  tickers. Use `first_seen_at` as the conservative `available_at` for a later
  forward experiment: an old publication date alone does not prove that trabe
  had the article before it was first collected.
- Validate HTTP(S) URLs and publication timestamps. Invalid/future timestamps
  should be flagged or rejected, not silently converted to the receipt time.
  Strip only known tracking parameters/fragments when canonicalizing URLs;
  retain article-identifying query parameters.
- Recommended bounds: retain at most 20 recent items per symbol and at most
  1,000 unique articles overall; cap headline/summary/URL text sizes. Apply the
  existing three-calendar-month expiry as an additional upper age limit.
- Keep compact daily per-symbol signals for three months: as-of time, article
  counts, sentiment counts/score, method version and the top source references.
  This preserves useful advisor inputs without accumulating every article.
- A news refresh should update cache/signals only. Do not send one unsolicited
  Telegram notification per headline; the daily digest is the proactive output.

Use Finnhub first. If selected fallback is enabled and Finnhub is denied or
unavailable, try Yahoo RSS with independent pacing/cooldown and display the real
source and fallback reason. Never represent a failed fetch as “no news.” Keep
previously cached items and expose their age and the last error. Yahoo can change
or rate-limit its feed, so failure of both sources must remain visible.

## Suggested feature interfaces and Telegram controls

`News(store, signals_http)` should provide:

- `refresh(symbols, *, now, manual=False, limit=5)`: bounded network batch,
  persistent per-symbol cursor/TTL and errors; returns a structured outcome.
- `recent(ticker=None, *, limit=5, as_of=None)`: local data only.
- `report(ticker=None)`: concise source-linked summaries and heuristic sentiment.
- `signals(symbols, as_of)`: structured records eligible at that timestamp.

`Briefing(store, news, earnings, insiders, portfolio)` should provide:

- `render(now)`: a cached, consistent combined report; no market/API work.
- `schedule(owner_id, outbox, now)`: one atomic daily claim plus outbox enqueue.

Proposed commands are `/news [TICKER]`, `/news_refresh [TICKER]`, `/briefing`, and
`/briefing HH:MM|off`. Feature enable/provider settings can use the existing
validated `/set` mechanism or dedicated controls chosen in the implementation
plan. A refresh command must enqueue background work and reply immediately.
New automation and the briefing schedule start off; manual cached reports work
while automation is paused.

The briefing should include:

1. As-of timestamp, reporting currency and a compact holdings/value/P&L summary,
   with missing FX/prices or partial totals identified.
2. Upcoming earnings with certainty/time labels and held exposure where known.
3. Recent meaningful insider buys with transaction and filing/publication dates.
4. Three to five deduplicated top news stories, prioritizing held stocks, each
   with two-sentence summary, heuristic sentiment, source and link.
5. Source health/freshness gaps and enabled/disabled sections, rather than a
   blanket claim of complete coverage.

Use the existing owner timezone for the configured clock time and the US
calendar for session context. Run on verified trading days before opening;
same-day catch-up may run before the open, but do not replay missed previous
days or send a “morning” briefing after the session has begun. Manual `/briefing`
remains available at any time. Keep output near one or two Telegram chunks.

## Shared 0.5–0.7 architecture recommendation

The existing `request_json` and `Finnhub.call` reject arrays, and their automatic
gate requires price monitoring to be on. News/earnings/insider work must therefore
use a shared signal transport capable of bounded JSON list/dict, CSV and XML
responses, with its own feature gates. Do not tie these features to `/monitor on`
or the selected price provider.

Root owns a `SignalsHTTP` abstraction. Finnhub traffic must still share the same
`.finnhub.lock`, completion timestamp, cooldown and persistent provider budget
as quotes and monitoring. Recheck global pause, storage state and the relevant
feature toggle before every automatic request. Alpha Vantage fallback traffic
must use its existing separate budget and pacing, not a new uncounted allowance.

Use one independent background insights worker rather than putting network work
in Telegram dispatch or adding a thread per ticker. A cycle can take a bounded
number of requests, prioritize imminent earnings/held symbols/manual jobs, then
rotate a persistent cursor. A three-hour per-symbol news TTL is a reasonable
initial default: 50 symbols mean about 400 daily calls before other features,
comfortably below the app's current 2,000/day Finnhub local ceiling in an otherwise
ordinary workload. This is a planning estimate, not a vendor quota guarantee;
other callers and provider rate limits still apply.

Claim each briefing date and enqueue its `kind='summary'` message in one SQLite
transaction. Recheck pause/timezone/schedule/snapshot inputs there, following the
existing Portfolio pattern. A queue/delivery retry must not refetch news. Pause
must suppress proactive messages through the existing outbox while preserving
control replies. All scheduled event ledgers, fetch cursors and cooldowns survive
restarts. Delivery remains in the existing independent delivery worker.

## Focused acceptance checks

- Normalize Finnhub list responses and Yahoo RSS, including missing summaries,
  bad URLs/times, malformed payloads and source failures.
- One URL/source ID appearing across requests/tickers stays one article while
  retaining ticker associations; publication and first-seen dates remain distinct.
- Pure two-sentence extraction, headline-only fallback and labelled polarity/
  negation sentiment produce bounded deterministic outputs.
- Cached `/news` and `/briefing` consume no provider calls; manual refresh queues
  work; held-only coverage and source gaps are present in reports.
- A daily briefing combines portfolio/earnings/insider/news data, claims once,
  respects pause and date/session gates, and does not compare unavailable values
  or replay prior days after downtime.
- Retention/caps remove old news and compact signals without deleting the trade
  ledger. Use a few targeted fixtures, not a repeat of ARM or storage soaks.

“Ready to deploy” means these interfaces, commands and fallback behavior work
with fixture data and the verified free source. It does not require changing the
currently deployed Pi during development.
