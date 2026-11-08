# trabe 0.7.0 — earnings, insider purchases and morning briefing

**0.7.1 presentation update:** Telegram reports now use bold headings/tickers and
italic notes. News shows bullish/neutral/bearish with signed bias, short headlines
and age; links and provider metadata stay saved rather than appearing in news
messages. Earnings messages omit source links; insider messages emphasize value,
roles and filing dates. The underlying 0.7 features and storage remain as described below.
See [message examples](messages.md).

Release candidate prepared 2026-11-04. This combines roadmap milestones 0.5–0.7.
The Pi remains on 0.4.0; this work has not deployed, committed or pushed anything.

## What is available

- Earnings schedules for held and watched stocks, configurable upcoming warnings,
  date-change corrections, source conflicts and cached position exposure.
- Meaningful CEO/CFO/director purchases, with filing links, amounts, transaction
  and publication dates, role checks and verified non-derivative eligibility.
- Company news with source links, bounded two-sentence extracts and explicitly
  labelled Bullish/Bearish/Neutral heuristic sentiment.
- An optional morning digest combining positions, cached prices, earnings,
  insider purchases and news. Manual refreshes run in the background, leaving
  `/status` and controls available.

Existing position/FX accounting, prices, dip rules, pause/resume, the private
Telegram interface and rootless Quadlet deployment remain in place. No new
runtime dependency, account, paid plan, webhook or inbound port is required.

## Telegram commands

| Command | Effect |
| --- | --- |
| `/earnings`, `/earnings VST` | View cached schedules, certainty, checked time and exposure. Queue a first fetch when coverage is missing. |
| `/earnings on`, `/earnings off` | Enable/disable standalone refresh and warnings. |
| `/earnings refresh` | Queue a new refresh for held and watched stocks. |
| `/set earnings_warning_days 3` | Warn within 3 calendar days; default 5, allowed 1–14. |
| `/insiders`, `/insiders VST` | View cached purchases, qualification and source links. |
| `/insiders on`, `/insiders off`, `/insiders refresh` | Control refresh/alerts or queue a manual refresh. |
| `/set insider_min_value_usd 25000` | Minimum purchase value in USD; default 10000, allowed 1000–1000000000. |
| `/news`, `/news VST` | View cached extracts, publication dates, sources and heuristic sentiment. |
| `/news on`, `/news off`, `/news refresh` | Control background news caching or request a refresh. No individual headline alerts. |
| `/briefing now` | Refresh all sections and send a manual digest. |
| `/briefing 08:00` | Enable a daily briefing at 08:00 in the saved timezone. |
| `/briefing on`, `/briefing off`, `/briefing` | Enable, disable or inspect its schedule. Default time is 08:00. |
| `/status` | Show feature switches, source refresh times/issues and queued work. |

All four new automation switches default **off**. An enabled briefing refreshes
all its sections even when their standalone automation is off; standalone
earnings/insider alerts still require their own switches. `/briefing off` stops
the digest and its collection unless standalone features remain enabled.
`/set briefing_time HH:MM` changes the time without enabling the digest.

Automatic briefings run once per local verified US trading day, after the saved
time and **before the US core session opens**. Holidays/weekends are skipped.
A restart after the open does not replay a missed morning digest; `/briefing now`
works at any time. A cold cache gets up to five minutes to warm, then the digest
reports any missing sections. Calendar coverage currently ends in 2028.

Pause suppresses automatic collection and queued proactive messages. Manual
cached views and explicit refreshes remain available. A request or message already
in flight may finish. Insider alerts start with filings published after activation
or resume, avoiding a historical backlog. Event claims and proactive outbox inserts
are atomic and survive restart. Earnings date corrections have distinct claims.

## Sources, limits and freshness

| Data | Primary | Fallback/limits |
| --- | --- | --- |
| Earnings | Finnhub calendar, queried per symbol, cached 24 hours | Alpha Vantage bulk CSV, shared once/day when needed. Dates are provider-reported expectations, not company-confirmed schedules. |
| Insider purchases | Keyless Tracefour filings, cached 6 hours | Finnhub cross-check required to verify non-derivative purchases. Unverified data stays visible without an alert. Tracefour attribution/link is retained. |
| News | Finnhub company-news, cached 3 hours | Yahoo ticker RSS, best effort with an independent retry interval. Source failures and fallback are visible. |

All Finnhub REST requests share the existing minute/day allowance, pacing and
cooldowns with price work. Alpha fallback shares its existing budget; failures
also consume attempts. Public feeds have persisted request limits and cooldowns.
A refresh can be partial when an allowance, provider restriction or outage is
encountered. `/monitor on` and the selected price source do not gate these feeds.
Manual refresh bypasses the feature cache but never bypasses request budgets.

Tracefour's documented anonymous allowance is 60 calls/hour/IP; trabe conservatively
limits itself to 40. Its response is capped at 500 filings with no documented
pagination, and its ticker format supports 1–5 letters; incomplete or unsupported
coverage is reported. A verified SEC code P is described as an **open-market/private
purchase** when the execution venue is unproven. It is not proof of an exchange
purchase. Known grants, derivatives, paired exercises, withholding and offering
flags do not qualify. Amendments are retained without another purchase alert.

News extracts use supplied snippets/headlines, with a headline-only label when
needed. Sentiment is a versioned local lexicon estimate, not an AI forecast or
premium vendor score. No full-article scraping or model download runs on the Pi.
Direct SEC enrichment, paid sentiment and optional model summaries are later work.

Research and observed free-access evidence:
[earnings](research-0.5-earnings.md), [insiders](research-0.6-insiders.md),
[news](research-0.7-news.md). Access was verified with the current credentials;
providers can subsequently change availability or limits.

## Storage and upgrade

Schema **4 → 5** adds signal events, source health, durable refresh jobs,
notification claims and HTTP cache/accounting. Existing ledger records,
historical FX, preferences, watchlist, quotes and pending replies are preserved.
Transaction records and their saved FX remain independent of market-data expiry.

Events keep event/publication, first-observed and availability timestamps plus
source/method metadata for the later advisor. These are inputs, not a predictor.
News daily signals freeze an as-of snapshot; later article edits do not become
earlier knowledge. Compact signals expire after three calendar months. Future
earnings remain available even if first discovered earlier; old refreshed filings
do not acquire another three-month lifetime merely by being fetched again.
Row caps, bounded payloads and existing disk-pressure controls also apply; see
[storage policy](storage-and-retention.md#earnings-insiders-and-news-in-07).

Use the existing `bash scripts/deploy.sh` when ready. It builds once on the Pi,
backs up SQLite before migration and retains its normal readiness, market,
restart/persistence, delivery and zero-published-port checks. A failed rollout
restores the previous image/configuration and pre-upgrade database. The existing
`stock-agent.service`, volume and deployment paths are intentionally retained.
No administrator step is introduced by this release.

## One walkthrough after deployment

1. Send `/status`, then `/earnings VST`, `/insiders VST` and `/news VST`. A first
   request may queue a fetch; its report arrives separately. Check source times,
   qualification, links and explicit gaps rather than expecting a particular event.
2. Send `/briefing now`; inspect holdings, cached prices and the three sections.
   This does not record transactions or enable scheduled automation.
3. Opt in as wanted: `/earnings on`, `/insiders on`, `/news on`, and
   `/briefing 08:00`. Use `/settings` to inspect the saved timezone and switches.
   Insider activation seeds existing filings without sending historical alerts.
4. Send `/pause 10m`, then `/news VST` to verify a manual cached view still works.
   Send `/resume` afterward. Disable unwanted automation with the matching `off`.

This walkthrough is the remaining live acceptance step after deployment. Do not
start a local instance with the same Telegram token. Local evidence and its
limits are recorded in [validation](validation.md#07-candidate-2026-11-04).
