# 0.8.6 — market context and richer briefing evidence

Todo steps **3 and 4** are complete, deployed and verified on the Pi. The owner
authorized both autonomously. Source/schema versions: 0.8.6 / 7. A commit message
for the combined uncommitted work is prepared in `.local/commit-message-0.8.6.txt`;
no commit or push was made.

## Premarket and VIX

Briefings refresh Yahoo public chart observations in the background for held/
watched stocks and the owner-selected **SPY/QQQ**. Display indicative premarket
minute-bar closes, change against the verified previous US session close, source
and observation time in the saved timezone. Timestamped prior-session closing
observations take precedence over ambiguous rolling metadata; never use the
multi-day chart's `chartPreviousClose` as yesterday's close.

Prices are labelled indicative, with unverified equity-feed delay. Premarket
starts at 04:00 New York on verified trading days; early/weekend briefings show
previous-close references and unavailable current premarket observations. Old
bars, failed requests, missing baselines and regular-session references remain
explicit. Future cache observations are excluded. These inputs do not replace
the existing position valuation source or become executable trade prices.

VIX is the actual `^VIX` index in **points**, with point/percentage change against
the dated prior index close, source timestamp and **15m delay** label. Prior-session
and stale values stay visible as such. No VIX-linked ETF/futures substitution or
directional prediction is made. The [source research](research-market-context.md)
records observed free access and an independent Cboe value/baseline check.

Active premarket/index context refreshes about every 15m, otherwise about every
six hours. Manual `/briefing now` requests a refresh; repeated benchmark symbols
are fetched once per job. Automatic context runs only with the briefing enabled
and respects pause/storage controls. Charts share Yahoo's existing app lock,
cooldown and 120-attempt rolling-hour allowance, with a 60-chart sublimit reserving
headroom for RSS/articles. Responses cap at 2 MB; current context stores one
compact record per symbol, capped at 1,000 globally under existing retention.

## Shared article evidence and broad-market news

The earnings-specific public Yahoo reader now shares `ArticleCache` with company
and market news. Require a supported public Yahoo article URL, one Article/
NewsArticle metadata object, valid publication time and real article text.
Scripts/SVG/style content remains excluded. Cache at most 32 parsed documents,
128 KiB each, with 100,000-character text bounds, six-hour freshness, one-hour
negative caching and seven-day expiry. Fetch at most two new articles per company
refresh or market-news pass using the shared Yahoo budgets. Unsupported/blocked
pages use the available feed evidence; no model or paywall bypass is involved.

Selected Yahoo RSS is now collected alongside Finnhub, giving article enrichment
a normal path even when the primary feed works. Article observations have a
separate provider/item identity. Feed-native snippets/vendor annotations remain
intact, syndicated copies count once, and cached articles do not outrank newer
corrected headlines/figures. Grouping is versioned `quality-v2`; old finalized
snapshots are unchanged. `/news TICKER` and briefing company cards distinguish
**Article extract**, **Feed extract** and **Headline only** while retaining source
URLs/publication/receipt provenance in storage.

When valid article evidence is older than the leading stories, show one dated
**Additional article context** item instead of hiding it or changing the main
story ranking. Extracts omit publisher author/read-time headers. The first live
walkthrough exposed this visibility gap; it justified one focused fix and second
image rollout, followed by a cached presentation/delivery check that reuses the
successful source refresh.

The separate market-news section fetches Finnhub general news, with the verified
Yahoo index RSS path as fallback. It selects macro/index headlines, excludes
obvious company promotions/opinion bait, deduplicates copies, stores at most ten
stories and displays up to three grounded extracts. Requests are at most hourly
automatically, inside existing provider budgets/preferences. Missing/stale/partial
coverage is explicit. It does not create synthetic company sentiment or forecasts.

Earnings uses the same parsed document cache while preserving exact issuer,
quarter/year, publication window, EPS basis/currency and conflicting-value checks.
News EPS still has no unverified beat/miss comparison; calendar forecasts remain
separately labelled. Publication, document receipt and current check times remain
distinct. Existing finalized v1 packets remain unchanged. Market context and the
broad-market section are not added to the decision-packet schema; the future
outlook stage must version/capture the exact new market inputs it consumes.

## Verification and deployment

Focused cases cover market baselines/session rollover/future data, public request
sharing/cooldowns, article cache reuse/bounds/cancellation, strict earnings checks,
revision stability/corrections/vendor attribution, market-news filtering/fallback,
shared controls and snapshot immutability. Relevant passing results are reused
for unchanged code. Changed-file Ruff and metadata/document checks pass.
The first rollout passed readiness/delivery/restart checks and normal briefing
job **31** completed with all eight market observations and eight stored macro
candidates. It also exposed the article visibility gap described above. That
concrete failure justified a focused fix and second image, rather than a routine
validation replay. Final image: **build-7e2e329ac99b0049**; its readiness, delivery,
market API and restart/persistence checks passed.

The corrected cached briefing was delivered with dated VST article context and
cleaned extracts. It reused the successful source refresh, with **zero new article
fetches**. VIX was **25.64 points**, prior-session and 15m-delayed; the Sunday view
showed November 6 closes for five stocks/SPY/QQQ and no current Monday premarket
bars. All nine changed module hashes matched the workspace and delivery drained
without error. This does not claim a live Monday premarket observation on Sunday.

Evidence: `.local/deploy-0.8.6.log`,
`.local/deploy-0.8.6-context-fix.log`,
`.local/market-context-probe/first-live-evidence.json` and
`.local/market-context-probe/walkthrough-0.8.6.json`. Focused results are reused
for unchanged code; there was no extra real Telegram poller. Prediction/advisor
and paper execution remain later work.
