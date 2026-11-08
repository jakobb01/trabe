# Next implementation: market briefing and daily outlook

Updated 2026-11-08. Status: **owner authorized steps 3 and 4 to completion**.
Steps 1 and 2 were deployed in 0.8.5. Steps 3 and 4 are complete, deployed and
verified in 0.8.6. Commit message prepared in `.local/commit-message-0.8.6.txt`;
no commit or push was made. Later steps remain planned.
See the [roadmap](docs/next-features.md) and [handoff](docs/project-status.md).

Sequence: close insider coverage checkpoint → local market-open time →
free premarket/VIX inputs → richer news → short daily outlook → broader advisor
→ paper portfolio/evaluation. The existing earnings fixes and news/snapshot
foundation are implemented; this checklist covers the next work.

## 1. Close the insider coverage checkpoint

- [x] Review the existing 0.8.2 coverage fixes and saved live evidence against the
  owner's remaining coverage concern. Identify missing filings, qualification
  metadata, partial ranges or source errors; a healthy empty feed is distinct.
- [x] Address a demonstrated remaining gap within the current sources first.
  If another API is required, explain the exact gap and verify a free option
  before enabling it. Do not buy coverage or weaken purchase qualification.

Done when remaining limitations and a concrete next action, if needed, are clear;
do not repeat the already completed deployment walkthrough without a new reason.

Completed: [coverage findings and limits](docs/insider-coverage-review.md).
Retain 500 primary rows per ticker, inspect them before snapshot qualification,
preserve independent source coverage times/counts and report unusable rows.
No new API needed. Passing: 21 insider cases, 15 snapshot cases and changed-file
Ruff. The deployed VST refresh confirmed three eligible purchases, independent
source receipt times and no unusable rows/partial coverage.

## 2. Show market-open time in the owner's timezone

- [x] Render the verified US session open in the configured IANA timezone, with
  its local date/time and zone, plus useful session status/countdown in briefing.
- [x] Reuse `/set timezone ...` for travel and make the active timezone clear.
  Do not assume Telegram automatically reports timezone changes.
- [x] Use exchange-session dates for scheduling/deduplication and convert them
  to local display times. Check DST mismatch weeks, holidays/next session,
  early closes, calendar expiry and zones whose local date differs from New York.

Done when the market-open line and briefing schedule agree with the saved
timezone and verified exchange session, including after a timezone change.

Completed: local open/close/countdown and next verified session, active
timezone in the header, and exchange-session claims. HH:MM is on the local date
of that open and must be earlier than the open. Passing: nine briefing-time
cases, 16 shared controls and the existing intraday DST/early-close case; Ruff
passed. The combined live check delivered a Europe/Ljubljana briefing showing
the next open as November 9 at 15:30 CET. Rollout readiness, delivery and restart/
persistence checks passed. See [0.8.5 details](docs/release-0.8.5.md).

## 3. Verify free data access; add premarket prices and VIX

- [x] Check existing providers first for actual premarket observations and VIX
  coverage. Verify timestamps, feed/session semantics, delay, previous close,
  request limits and access from the Pi. An HTTP success alone is insufficient.
- [x] If coverage needs another free API/key, report the gap and proposed source
  to the owner before enabling it. Unavailable access stays visible.
- [x] Add premarket price and change versus previous regular-session close for
  held/watched symbols and an agreed small set of broad-market references.
  Show currency, source, observation time and whether data is delayed/stale.
  Keep previous closes and regular-session quotes explicitly labelled.
- [x] Add VIX level, change, source and observation time. Identify the change
  baseline and last-session/delayed data; use VIX as volatility context, without
  deriving market direction solely from its level. Never silently substitute a
  VIX-linked product or futures price for the index.
- [x] Reuse background refresh, persistent cache, provider budgets/cooldowns and
  pause controls. A briefing with one unavailable input should still be useful.
- [x] Check that an early scheduled briefing handles absent premarket/VIX
  updates honestly; do not present yesterday's observations as today's live data.

Done when the briefing shows source-qualified market inputs or explicit gaps
within free access and bounded request usage.

Complete: Yahoo chart observations for held/watched stocks and the
owner-selected SPY/QQQ, plus the actual VIX index (15m delayed). No new key or paid
API. Equity prices are labelled indicative minute bars with unverified feed delay;
early/weekend briefings show prior closes and explicit current-premarket gaps.
Charts share Yahoo's app allowance/cooldown, with 60 chart attempts/hour inside
the shared 120/hour ceiling to preserve news headroom. Twelve focused cases plus 16
shared controls and nine clock cases passed. The live briefing job refreshed all
five current stocks, SPY, QQQ and VIX; the Sunday view correctly showed prior
closes and unavailable current premarket bars. VIX remained 25.64 points with its
delay/prior-session labels. [Source and release evidence](docs/release-0.8.6.md).

## 4. Improve news evidence for the briefing

- [x] Extend the earnings-specific Yahoo article reader into a shared bounded
  article cache for supported public articles from the existing news feeds.
  Preserve the earnings parser's strict evidence checks.
- [x] Add a small broad-market news view alongside held/watched-company news;
  verify that selected free feeds actually cover it. Do not infer the whole
  market's news from only the current company watchlist.
- [x] Produce compact extracts with publication/receipt provenance, source links
  retained for inspection, syndicated-story deduplication and explicit
  article-backed versus headline-only evidence. Reuse cache and request budgets.

Done when the briefing has a few distinct, current market drivers with traceable
evidence and visible coverage limits. No new model is a prerequisite for this step.

Complete: shared Yahoo ArticleCache reused by earnings/news, selected RSS
collected with the primary feed, separate article observations and labelled
extracts, plus filtered/deduplicated Finnhub general news with Yahoo index RSS
fallback. Eleven article regressions, five market-news cases and the relevant
existing news/earnings/control/snapshot cases pass. Strict EPS checks and existing
snapshots are preserved. The live check found article context hidden below the
leading stories; its focused fix adds one dated supplementary extract. Final
cached delivery showed it without repeating provider requests, and all nine
module hashes matched the workspace. See [0.8.6 details](docs/release-0.8.6.md).

## 5. Add a short daily market outlook

- [ ] Agree a small market benchmark set and fixed horizon for the first version
  (proposed: opening tone and today's US regular session); define how to assess
  each issued expectation before collecting outcomes.
- [ ] Combine available broad-market/company news, premarket direction/breadth
  and VIX into one or two short sentences: expectation, main reason and risk or
  condition that could change it. Label the inference separately from facts.
- [ ] Support bullish/bearish/mixed or insufficient-evidence output. Conflicting,
  missing or stale inputs reduce specificity; no invented probability or
  numerical confidence from news bias or categorical provider labels.
- [ ] Save the issued outlook, as-of time, target session/horizon, source input
  identities/timestamps, gaps and rule/model version before outcomes arrive.
  Define bounded retention and version new snapshot fields; never rewrite older
  packets or apply subsequently received news to an earlier prediction.
- [ ] Keep the Telegram message compact and assess issued expectations against
  their declared benchmark/horizon. Evaluate forecast usefulness separately from
  simulated trading returns.

Done when the owner receives a brief, grounded expectation for today and its
record can later be compared fairly with what happened.

## 6. Expand the advisor, then build paper evaluation

- [ ] Extend timestamped market outlooks into company-specific research/action
  suggestions, with explicit horizons, reasons, risks and missing-data handling.
- [ ] After the advisor, implement a separate paper ledger with agreed execution,
  costs, benchmarks and experiment retention. Follow the roadmap's existing
  no-future-input and realistic-fill rules.

## Delivery and verification

Keep changes small and usable in Telegram. For implementation, run focused
behavioral cases and changed-file lint/format checks under
[CONTRIBUTING.md](CONTRIBUTING.md). Use fixtures; keep only the deployed process
polling the real bot. Each deployed increment uses the existing rollout checks
and one relevant walkthrough. This planning update needs diff/link review only.
