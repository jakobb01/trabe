# trabe conversation handoff

Updated 2026-11-08. Read this, AGENTS.md, CONTRIBUTING.md and the roadmap before
continuing. Verify the worktree and saved deployment evidence. Preserve existing
uncommitted work and update this note after meaningful changes.

## Current work and Git

- The owner next authorized todo steps 3 and 4 autonomously, then preparation of
  a commit message (no commit/push). The single research-only subagent completed
  its pipeline review; no agents are running. Steps 3 and 4 are complete, deployed
  and verified
  in source **0.8.6**, schema 7. The first 0.8.6 Pi rollout passed its operational
  checks, and normal briefing job 31 completed/delivered with all eight market
  observations, VIX 25.64 and eight stored macro candidates. The feature check
  caught a presentation gap: valid article groups existed, but were below the
  three leading stories and invisible in cards. A focused follow-up adds one
  explicitly aged supplementary article context, strips publisher read-time
  headers, and excludes Treasury-agency-only stories from macro drivers.
  Eleven article cases, affected news/earnings checks and five market-news cases
  cover this fix. The second image passed readiness, market API, delivery and
  restart/persistence checks. The final cached briefing showed dated VST article
  context without read-time headers, zero new article fetches, no pending messages or
  delivery error, and all nine deployed module hashes matching the worktree.
  Evidence: `.local/deploy-0.8.6-context-fix.log` and
  `.local/market-context-probe/walkthrough-0.8.6.json`. Final image tag:
  `build-7e2e329ac99b0049`. Reuse the completed source refresh and walkthrough;
  do not repeat them without a new reason. Commit message is prepared in
  `.local/commit-message-0.8.6.txt`; no commit or push was made.
  `market_context.py` adds Yahoo indicative premarket bars for held/watched stocks
  plus owner-selected SPY/QQQ, and 15m-delayed VIX in points. Pi access evidence
  `.local/market-context-probe/access-pi.json` shows actual VST/SPY premarket rows
  and VIX 25.64 matching Cboe's last daily close; Massive stock/index snapshots
  returned 403, and Finnhub's VIX quote was unusable. No new key/paid API.
  Charts use persistent bounded current observations, source/session timestamps,
  verified previous-session close selection (never multi-day chartPreviousClose),
  shared Yahoo accounting/cooldowns and 60 chart attempts/hour within the shared
  120/hour app limit. Early/stale/missing/prior-session values stay labelled.
  Passing: 12 market-context cases, 16 shared controls and nine clock cases.
  Step 4 adds `articles.py` (32 parsed documents, 128 KiB/document, six-hour cache,
  one-hour negative cache, seven-day expiry, two new fetches per company/pass)
  reused by earnings and news. Selected Yahoo RSS now runs alongside Finnhub;
  separate `yahoo-article` observations preserve original feed/vendor provenance.
  New grouping quality-v2 prefers article extracts but lets newer feed corrections
  outrank stale cached articles. Company detail/briefing cards label evidence.
  `market_news.py` supplies selected Finnhub general-news drivers and Yahoo index
  RSS fallback, capped at ten stored/three displayed stories. Pi checks verified
  both feed paths; evidence: `.local/market-context-probe/news-access-pi.json`.
  Passing: 11 article cases, five market-news cases, 30 existing news cases,
  five strict earnings-news cases and 15 snapshot cases. Affected shared controls,
  Ruff and document/metadata checks passed. [0.8.6 details](release-0.8.6.md).
  Source and directly verified Pi version are **0.8.6**, schema **7**. Daily
  outlook/advisor and paper execution remain planned; neither was started.
- The owner authorized the first two market-briefing steps sequentially. Insider
  review/fixes completed before local market-open time; both are deployed and
  verified in 0.8.5. The read-only Pi review found three verified VST buys and no
  P purchases for GOOGL, AMD, AMZN or
  NVDA, with healthy sources/no partial-range flags. Fixes retain up to 500 primary
  purchases, inspect these before snapshot qualification, preserve source-specific
  coverage counts/times through failures and expose unusable filing rows. No new
  API needed. Passing: 21 insider cases, 15 snapshot cases and changed-file Ruff.
  [Findings and remaining limits](insider-coverage-review.md). The timezone step
  shows the active zone and local open/close/countdown/next verified session.
  HH:MM uses the local date of the open; claims/warm-up use the US session date.
  Passing: nine new clock cases, 16 shared controls and the intraday DST/early-close
  case, plus Ruff. [0.8.5 notes](release-0.8.5.md).
- Previous verified rollout: **0.8.5**, schema **7** (now superseded by 0.8.6).
  `.local/deploy-0.8.5.log` ends in successful readiness, Telegram delivery and
  restart/persistence checks. Final image tag: `build-53abe2b947546a9d`.
- The combined live walkthrough completed a VST insider job with three eligible
  purchases, no rejected rows/partial coverage, and distinct Tracefour/Finnhub
  receipt times. A delivered briefing used Europe/Ljubljana and showed the next
  open as 2026-11-09 15:30 CET, close 22:00 CET. Delivery drained without error;
  all four changed module hashes match the worktree. Evidence:
  `.local/earnings-current-probe/walkthrough-0.8.5.json`. Reuse the completed
  rollout and walkthrough; do not repeat them without a new reason. The remaining
  market-briefing steps are planned and were not started.
- 0.8.4 fixes hidden estimates on Yahoo result cards. The Pi still held EPS
  forecast 2.991 and revenue forecast 6084287722; the renderer omitted them.
  Reports, earnings briefing cards and future news-result alerts now show a
  separate labelled calendar-estimates line. It does not populate the news
  paired-estimate field or add an unverified beat/miss grade.
  [Release details](release-0.8.4.md). Focused checks: 37 earnings and 16 shared
  control cases, plus changed-file Ruff. Unchanged earlier checks remain reusable.
- The live 0.8.4 check delivered a cached VST report containing
  `Calendar estimates (Finnhub): EPS 2.991 • Revenue 6.08B`; the earnings briefing
  card contained the same line. News EPS stayed 3.25 USD with no paired estimate,
  both changed source-module hashes matched the worktree, and delivery drained
  without error. Evidence: `.local/earnings-current-probe/walkthrough-0.8.4.json`.
  Do not rerun the completed check unnecessarily.
- The user confirmed the public Yahoo article reader can be a foundation for
  better news and briefings. Broader shared article caching/enrichment is now in
  the roadmap; this patch improves the earnings section of briefings immediately.
- New requested briefing work is recorded in the
  [market briefing checklist](../todo-market-briefing.md): premarket prices,
  US market-open time in the owner's current configured timezone, VIX and a
  short daily market prediction based on those inputs and news. Proposed order:
  close insider coverage checkpoint → local market clock → verify free access
  and add premarket/VIX → richer market news → daily outlook → broader advisor
  → paper evaluation. Steps 1–4 are complete; later stages remain planned. See
  the current progress above and checklist for implementation status.
  Briefing timestamps/schedule and (from 0.8.5) market session times use the saved
  timezone. Telegram does not supply travelling users'
  current timezones automatically; reuse the existing timezone preference.
- Last checked Git commit: `cbedf2b` (earnings, insider purchases and briefings).
  The earlier 0.7.2/0.8 work and these patches remain uncommitted. The owner has
  been handling commits/pushes; no assistant commit or push has been made.
- The user requires **free sources only**. Do not suggest enabling a paid
  subscription as though it were authorized. No subagents are running.

## Earnings incident: read before trusting provider figures

Finnhub's successful live VST refreshes still returned null Q3 actuals and history
ending at Q2. The Pi was confirmed running 0.8.1 with earnings enabled and recent
successful requests: this was a real data gap, not just stale local storage.

The attempted 0.8.2 Alpha Vantage fallback returned **1.75 vs 2.94** for VST Q3
2026. The owner correctly challenged the 1.75 figure against Yahoo news showing
3-something. Checking HTTP success and fiscal labels did not establish financial
accuracy. The false comparison was withdrawn from live records and a correction
queued to the owner. It remains under `withdrawn_results` for audit; existing
notification claims and finalized snapshots are not erased or rewritten.

Alpha result ingestion is **disabled by default** and the Pi has persistent
`earnings_alpha_disabled=1`. Do not reactivate it using the old access check or
parser tests as evidence. The experimental parser tests check mechanics only;
they do not validate the vendor's financial values. A temporary Alpha cooldown
was set during quarantine; the completed `.local/walkthrough_083_pi.sh` restored
the previous cooldown after verifying 0.8.3 and removed retired Alpha result-
source health checkpoints. The permanent result-ingestion block remains on.

Other free structured checks still ended at Q2: Nasdaq surprise history, Yahoo
financial time series, Finnhub metrics and Finnhub reported statements. The
reported statement was year-to-date, so do not treat it as standalone Q2 EPS.
SEC company facts returned 403 from Pi; do not evade that restriction. The
existing Massive key returned 403 for Benzinga earnings; news access does not
include that paid dataset. The user subsequently specified free only.

0.8.3 adds a **free Yahoo news-evidence fallback**. A live RSS-to-article check on
Pi, using a temporary database, extracted **basic EPS 3.25 USD for FY2026 Q3**.
The public article is accessible with the normal app client. Extraction requires
company identity, recent NewsArticle publication metadata, and an explicit
basic/diluted EPS line naming the exact quarter/year against its previous year.
It excludes scripts/SVG/price widgets, forecasts, other issuers, conflicting
figures and future or prior-quarter evidence. It checks at most two articles.
Unmatched articles leave a gap; this is not a general guarantee of earnings
coverage. It needs no key or paid API.

The bot labels the basis/source/currency and leaves the comparable estimate
unverified, with **no beat/miss grade**. Never compare this basic EPS with an
unverified calendar consensus. New snapshots retain the source URL, publication
and receipt times, basis/quality and withdrawn-observation audit. Distinct durable
correction keys allow the corrected news figure after a withdrawn EPS verdict.

Evidence (ignored, no credentials): `.local/earnings-current-probe/` contains
`pi.json`, `alpha-and-insiders.json`, `income.json`, `validation-sources.json`,
`yahoo-financials.json`, `free-sources.json`, `parser-live.json` and
`news-service-live.json`. See [0.8.3 notes](release-0.8.3.md) and
[original diagnosis](earnings-vst-diagnosis.md).

## Commands and insider coverage

Bare `/news`, `/insiders` and `/earnings`, or a ticker argument, queue a background
refresh and send the result. `cached [TICKER]` provides a no-network view. Existing
provider budgets/cooldowns apply. Pending earnings remain visible for seven days,
with hourly follow-up after day two. `/insider` aliases `/insiders`.

The 0.8.2 insider fix retains transactions beyond row 100: the live GOOGL response
had **174**, but the app previously discarded the rest. Crowded date ranges split
within seven calls / 2000 unique transactions per symbol. Remaining crowded or
failed ranges are partial, not complete coverage. Reports include the 90-day
window, source counts/check times, eligibility counts and gaps. Role, meaningful
USD amount, P code, non-derivative verification and amendment/dedup rules remain.

Live comparison found three VST purchases in both sources and zero GOOGL
purchases in both; a healthy empty purchase feed is not an outage. Current access
is sufficient for these concrete fixes. This does not establish complete market
coverage or turn unknown qualification into an alert.

## Verification and deployment

Relevant passing checks: **36 earnings**, **17 insider**, **16 shared controls**,
**31 bot**, **14 snapshots**, plus changed-file Ruff and shell syntax. Reuse
passing results for unchanged code. No routine broad suite, ARM/QEMU run,
container validation replay or storage soak is needed.

Deployment uses `bash scripts/deploy.sh`. It builds on Pi and checks readiness,
Telegram delivery, market API access and persistence after restart. Two 0.8.2
images passed rollout checks, but their Alpha EPS fallback was later rejected;
those passing operational checks do not validate the figure. The second fixed
receipt timestamps; a regression protects against backdating new observations.
The completed 0.8.3 rollout removes the unsafe default and adds tested news evidence.

The final walkthrough passed: earnings VST and insider GOOGL jobs completed on
the deployed worker, VST showed 3.25 USD basic news EPS with no paired estimate,
GOOGL retained **172 unique transactions / zero P purchases**, three requests
and no partial-range flag, and the delivery queue was empty with no delivery error.
Both earnings source-module hashes matched the worktree. Evidence:
`.local/earnings-current-probe/walkthrough-0.8.3.json`.
The helper never starts another Telegram poller. Do not rerun the completed
walkthrough unnecessarily, or run the obsolete 0.8.2 helper, which
asserts the rejected Alpha source. Deployment credentials/endpoints remain in
private `.env`/`.env.deploy`; do not print them. Pi access works with escalated
network permission here. Read-only helper: `scripts/diagnose-earnings.sh`.

## Product foundation and next priorities

trabe is a Python, owner-only private Telegram service on an ARMv7 Raspberry Pi
with SQLite and rootless Podman Quadlet. All app traffic is outbound; there is no
website, webhook, new inbound port, Rust rewrite or heavyweight local model.
The durable EUR/USD purchase/sale ledger, FX/progress, intraday dip alerts,
earnings, qualified insider purchases and briefings are implemented.

0.8 news quality, Massive categorical cross-checks and immutable snapshots are
implemented. The 0.8.1 local processing fix reduced a five-ticker/750-article
fixture from 35.7s to 1.4s; the user confirmed news works on the new update.
The signed news bias remains a local heuristic, not calibrated confidence or a
forecast. Do not average categorical Massive labels into that score. Marketaux
remains optional/unverified. Existing news/snapshot work is complete.

Snapshots freeze committed inputs in one transaction, with a canonical hash and
immutable final packets. Automatic capture defaults off, 30 minutes before US
open, and does not backfill more than five minutes late. Retention is three
calendar months automatic, seven days/32 records manual; packets cap at 256 KiB.
The ledger is durable. v1 does not include the entire price-history window.

Owner's order: **earnings correctness → insider coverage → news predictor/advisor
→ paper portfolio and evaluation**. The newly requested market inputs and short
daily outlook are proposed first advisor stages; see the checklist above for the
sequence. Steps 1–4 are complete; later steps still need implementation
agreement. No predictor or paper trading is implemented.
Before forecasts, choose the horizon, execution assumptions, benchmark, costs and
experiment retention. Capture the exact price window in future snapshot versions,
keep prior packets unchanged, and never evaluate using later news. See the
[roadmap](next-features.md) and [testing policy](../CONTRIBUTING.md).
