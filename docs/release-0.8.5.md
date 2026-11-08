# 0.8.5 — insider coverage and local market session times

These are the first two authorized [market briefing steps](../todo-market-briefing.md),
completed sequentially: insider coverage review/fixes, then local market-open
time. Premarket prices, VIX, article enrichment and predictions remain later work.

## Insider coverage

The [Pi coverage review](insider-coverage-review.md) found healthy feeds for the
five current tickers: three qualified VST purchases, no P purchases for GOOGL,
AMD, AMZN or NVDA. It also exposed local limits beyond the earlier Finnhub fix:

- Retain up to 500 primary purchase records per ticker instead of dropping
  everything after row 100. Global 5,000-row/three-month retention still applies.
- Scan the bounded retained set before selecting up to three qualifying insider
  facts for a new snapshot. Existing finalized packets remain unchanged.
- Preserve each source's counts and check time through an outage, label their
  freshness independently and retain actual filing receipt time separately from
  later verification. Show unusable filing rows and current cached eligibility.

No new API/key is required. Purchase qualification, money arithmetic, amendments,
activation/resume rules and durable alert deduplication still apply. The primary
500-row cap, unsupported share classes and bounded Finnhub date-range checks
remain visible limitations rather than a complete-coverage claim.

## Local session clock and scheduling

Briefings show the saved IANA timezone plus the verified US session's local open
and close times. Before the open they include a countdown; on weekends, holidays
and after the close they show the next verified open. Active sessions show their
open/close, including early closes. Conversion follows both US and local DST;
calendar expiry remains explicitly unavailable.

Use `/set timezone Europe/Ljubljana` (or another IANA zone) when travelling.
Telegram does not automatically report a changed timezone.

Automatic HH:MM briefing times apply to the **local date of the session open**.
Claims and cold-cache waiting follow the **US exchange session date**, so a
different local calendar day, restart or timezone change cannot duplicate the
same session's briefing. Choose an HH:MM before the local open: for example,
08:00 Los Angeles is after the US open, so it does not produce a pre-open digest.
`/briefing now` remains available at any time. Automatic schedules keep their
existing pause/enable controls and do not replay after the open.

For example, on 2026-11-27, Europe/Ljubljana shows an open at 15:30 CET and an
early close at 19:00 CET. Pacific/Auckland shows the same session on November 28
at 03:30–07:00 NZDT; a 03:00 briefing is still claimed for US session November 27.

## Verification and rollout

Passing focused checks: 21 insider cases, 15 snapshot cases, nine new briefing
clock/schedule cases, 16 shared-control cases and the existing intraday DST/
early-close case; changed-file Ruff checks/formatting passed. Reuse other passing
results for unchanged code. Schema remains 7; credentials and runtime state stay
outside Git. The Pi rollout passed readiness, delivery and restart/persistence
checks with image `build-53abe2b947546a9d`.

One combined live walkthrough refreshed VST insiders and delivered the updated
report and a briefing. It confirmed three eligible purchases, distinct source
receipt times, no rejected rows or partial coverage, and Europe/Ljubljana's next
open at 2026-11-09 15:30 CET / close at 22:00 CET. Delivery drained without error;
all four changed source-module hashes matched the workspace. Evidence:
`.local/deploy-0.8.5.log` and
`.local/earnings-current-probe/walkthrough-0.8.5.json`. No additional poller ran.
