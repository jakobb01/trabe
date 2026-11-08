# Additional news sources and sentiment cross-checks

Researched 2026-11-04; updated with the 0.8 implementation and a successful
Massive access check. Other new candidates remain documentation research. The design is
[news quality and snapshots](news-and-snapshots-plan.md); delivery tasks are in
[todo08-news-snapshots.md](../todo08-news-snapshots.md).

## Recommendation

The 0.8 candidate keeps Finnhub/local rules and implements **Massive first** as
a second article feed and vendor assessment. **Marketaux** is an optional numeric
adapter. New adapters require a key and access check before enablement; the
current Massive key has passed for GOOGL/VST. Marketaux remains off and unverified.
The rules view works without either new key.

The current bias comes from trabe's `lexicon-v1`, not Finnhub's sentiment product.
The existing key previously returned 403 for Finnhub `/news-sentiment`.
[Existing live evidence and contracts](research-0.7-news.md).

Distinguish three things:

| Layer | What another source improves | What it does not establish |
| --- | --- | --- |
| API vendor | Coverage and resilience when one feed fails. | Independent reporting if both distribute the same story. |
| Original publisher/reporting | Corroboration and different facts or perspectives. | A second scoring method. Syndicated copies remain one event. |
| Sentiment method | A second interpretation of the supplied evidence. | Correctness, calibrated confidence or a forecast of returns. |

A vendor score is a separately supplied assessment. Its model, training data and
upstream dependencies may be undisclosed; do not claim statistical independence.

## Candidates and verified limits

The limits below describe published plans, not measured latency, ticker coverage
or complete coverage. A bounded Massive check returned 62 GOOGL / 3 VST articles,
all with matching ticker sentiment. Evidence is private at
`.local/news-08-probe/access.json`; no full bodies or credentials are in that report.
Marketaux and Alpaca access remain untested. See the
[release walkthrough](release-0.8.md#configure-and-try-the-features-after-deployment).

| Source | Published free access / contract | Useful contribution | Decision |
| --- | --- | --- | --- |
| **Massive** (formerly Polygon), `/v2/reference/news` | Included in Stocks Basic; hourly updates; two years of history. Optional ticker-specific `insights` carry a sentiment label and explanation. Basic REST allowance is five requests/minute; reference endpoints may use a separate bucket. | More articles and a qualitative second assessment. Labels do not supply numeric strength. | Implemented in 0.8; the bounded GOOGL/VST access and insight check passed. [News contract](https://massive.com/docs/rest/stocks/news), [rate-limit policy](https://massive.com/knowledge-base/article/what-is-the-request-limit-for-massives-restful-apis). |
| **Marketaux**, `/v1/news/all` | Free: 100 requests/day, three articles/request. Entity annotations contain numeric `sentiment_score`, highlights and `match_score`; several symbols can be queried. | Numeric company-specific cross-check with supporting excerpts. Tiny pages constrain breadth. | Optional next adapter; do not depend on market-stat aggregates on Free. [Pricing](https://www.marketaux.com/pricing), [news contract](https://www.marketaux.com/documentation). |
| **Alpha Vantage**, `NEWS_SENTIMENT` | Current documentation marks it Premium. The general free allowance of 25 requests/day does not grant access to every dataset. Comma-separated tickers filter articles mentioning all listed symbols. | Potential later ticker sentiment source using an existing vendor account. | Exclude from the free baseline unless the actual key is explicitly verified to have access. [Endpoint](https://www.alphavantage.co/documentation/#news-sentiment), [free allowance](https://www.alphavantage.co/support/). |
| **Alpaca news** | Current historical API documents symbol/time filters, summaries and optional content. Its 2022 launch offered a limited-time free beta; that announcement does not verify today's entitlement. | Additional feed; current contract does not provide a built-in sentiment score. | Reserve candidate. Confirm current feed, access and limits first. [Current documentation](https://docs.alpaca.markets/us/docs/historical-news-data), [original announcement](https://alpaca.markets/blog/introducing-news-api-for-real-time-fiancial-news/). |
| **GDELT DOC 2.0** | Public API; article search and tone outputs. Tone uses an emotional scale of −100…+100. | Company-name discovery and broader sector/macro context. | Later best-effort discovery. Emotional tone is not ticker bullishness; do not rescale it into trabe's bias. [Official API description](https://blog.gdeltproject.org/gdelt-doc-2-0-api-debuts/). |
| **Yahoo RSS / issuer IR feeds** | Yahoo already serves as our best-effort fallback. Issuer feeds require per-company discovery. | Additional headlines or original company announcements. | Retain Yahoo fallback; add a bounded issuer-feed registry later. Neither provides a second sentiment model. [Existing research](research-0.7-news.md). |

Massive insights are optional: a successful response without them is news
coverage, not a sentiment cross-check. Preserve categorical positive/neutral/
negative results; do not convert “positive” into a fabricated +1.0 strength.
Hourly update frequency is unsuitable for promising immediate breaking-news alerts.

Marketaux defines entity sentiment as the average of sentiment in highlighted
text. Its entity matching strength is a different field: documented `match_score`
examples exceed 1, so it is not a probability. Provider similarity groups are
hints; trabe still needs cross-provider grouping. These contracts do not establish
GOOGL/VST completeness or comparability with our own bounded snippet analysis.
[Entity field definitions](https://www.marketaux.com/documentation).

## A small, bounded collection policy

Use provider adapters behind the existing outbound HTTP worker. Keep REST calls
out of Telegram handlers; this needs no webhook, inbound port, new website or
new news WebSocket. Missing credentials skip an adapter visibly; local rules and
existing sources keep working.

Proposed initial settings, subject to the account check:

| Adapter | Collection policy | Budget / partial coverage |
| --- | --- | --- |
| Finnhub | Keep existing three-hour cache and shared pacing. | Existing Finnhub allowance also serves other features. |
| Massive | One-hour cache; bounded ticker/time-window requests. | Start at four requests/minute globally across our Massive calls and 200 attempts/day as an app limit. Honor server bucket limits/cooldowns. |
| Marketaux | Three-hour cache; at most six scheduled checks per ticker/day. | At most 80 attempts/day, reserving 20 of the published free allowance. Pages, retries, manual refresh and metadata checks all count. |
| Yahoo RSS | Existing fallback behavior. | Best effort, not an additional continuously polled feed by default. |

For Marketaux, one page per ticker on each of six checks is:

| Tickers | Requests/day | Fits the proposed 80/day app limit? |
| --- | --- | --- |
| 4 | 24 | Yes, before retries/metadata. |
| 10 | 60 | Yes, with limited headroom. |
| 50 | 300 | No; reduce checks and rotate coverage. |

These are request counts, not unique article counts: three returned articles can
be the same three across repeated checks. Multi-symbol queries can spend fewer
requests, but one busy ticker can occupy the whole page. Never promise that one
batch refresh checked every requested company. Massive's proposed 200/day app
cap also requires rotation for a large watchlist.

Prioritise held companies, then rotate due watchlist symbols so they do not
starve. Maintain persistent attempt counters, cache timestamps and cooldowns per
provider. Defer instead of exhausting budgets on pagination/retries; no positive-
or negative-only query filters. Keep `not attempted`, `budget deferred`, `failed`,
`empty response` and `truncated` distinct. A successful empty result means only
“no matching articles returned by this feed/query,” not proof of no company news.

Keys remain in the private bootstrap environment; ordinary enable/limit settings
remain in Telegram. Never log token-bearing query URLs, persist API credentials
inside article URLs or follow pagination to an arbitrary host. Reuse existing
redaction, and cover `api_token` as well as `apiKey` when adapters are added.

## Compare assessments without manufacturing consensus

Run the same version of our rules analyser over eligible inputs from every feed.
That supplies one consistent company-level bias. Keep each vendor assessment
separately with its target company, input basis, receipt time and raw scale/label.
Do not average local and vendor numbers or count agreeing API vendors as extra
events. A future model adapter remains a separate optional experiment.

Pair comparisons only for the same company and a conservatively matched story/
event with materially compatible evidence. Prefer the exact same supplied text;
mark comparisons based on a vendor's unavailable full text or different excerpt
as different-context checks. Do not call different article baskets a model
agreement result. No overlap means “no matched stories,” not disagreement.
Apply our company-relevance check to vendor annotations too: a positive analyst
comment about the economy does not establish bullish news about the analyst's
employer merely because its ticker was tagged.

Compare direction using versioned method-specific thresholds. Keep the current
local numeric score visible. A categorical vendor result supplies direction only;
numeric vendor results retain their own scale and method. Neutral disagreement
and opposite-direction disagreement stay distinct. If matched stories differ,
show mixed rather than reducing them to an unexplained majority vote.

Illustrative planned Telegram output, with invented values and no source links:

```text
📰 VST • Bullish (+0.5)
2 distinct stories • guidance raised; demand improving
Second view: agrees on direction • 2 matched stories
```

Other states: `Second view: mixed`, `differs`, `no matched stories` or
`unavailable`. Do not turn missing vendor data into Neutral (0.0). A comparison
against one story must not look like a cross-check of the entire company view.

## Persistence and daily snapshots

Extend the proposed news schema with `news_provider_observations`. Canonical URL
deduplication must preserve each vendor's article identity, original publisher,
exact bounded headline/snippet and input hash, entity association and observation
time. Content revisions belong to a provider/item stream: different vendors'
snippets are input variants, not repeated corrections caused by alternating polls.
Select an event representative by a versioned input-quality rule. Keep changed
vendor annotations as timestamped occurrences even when the
headline is unchanged; repeated identical polls update only last-seen metadata.
Bound retained annotations/highlights and keep raw API response bodies out of Git.

Bind vendor scores through `news_analyses` with `origin=vendor`, observation ID,
target, raw result, scale/label, evidence basis and annotation availability time.
Record an undisclosed vendor model version as unknown. Never claim its score was
computed from our exact snippet unless the API establishes that. Losing a second
vendor annotation when merging duplicate URLs would defeat the cross-check.

Daily snapshots copy the local aggregate plus compact vendor assessments,
agreement state, matched/unmatched counts and collection gaps. The source
receipt, ticker association and score binding must all precede capture. A vendor
score received tomorrow cannot be added to yesterday's saved packet. Full
articles and all polling responses are unnecessary. Existing snapshot byte caps
and three-month retention still apply; retain at most two vendor assessments per
saved evidence item and trim optional detail with an explicit truncation marker.

## Delivery order and small acceptance checks

1. Implemented revision/target/provider-observation storage and grouping, then the
   improved local rules view. Keep this usable without new keys.
2. Implemented Massive behind configuration. The current key passed one bounded
   read-only GOOGL/VST check: access, actual insight coverage, timestamps and
   response shape. Enable its second view after that evidence, without claiming
   the entire feed has been validated.
3. Implemented optional Marketaux with its own budget; require a bounded access check
   if a numeric vendor view is wanted. Do not require every researched adapter.
4. Implemented frozen assessments/coverage in daily snapshots; next is the paper
   portfolio and predictor stages already agreed.

Use a few provider fixtures alongside the planned news-quality examples: duplicate
stories from two vendors count once but retain both annotations; opposite entity
scores stay attached to the correct company; label-only strength stays null;
conflicting/missing scores remain visible; thin pages show limited coverage;
budget deferral and 429s do not trigger retry loops; later annotations cannot
rewrite snapshots. Include credential redaction and owner-only configuration
when those code paths change. No new large benchmark, soak or deployment is
required by this research.
