# 0.8.3 — withdraw the disputed EPS and add free news evidence

The 0.8.2 Alpha Vantage response labelled VST Q3 2026 EPS as 1.75. The owner
correctly challenged that figure: current Yahoo reporting labels basic EPS as
**3.25 USD**. Checking request success and fiscal metadata was insufficient to
validate the financial value. The 1.75 comparison and its miss verdict were
withdrawn from the live bot, with the old observation retained as audit metadata.
Alpha result ingestion is now disabled by default and explicitly blocked on Pi.
Do not reactivate it on the basis of the earlier parser tests or HTTP 200 checks.

The owner requires **free sources only**. Live checks found:

| Source | Observed limitation |
| --- | --- |
| Finnhub calendar/history, metrics and reported statements | Current VST EPS missing; financial statement data stops at Q2, with a year-to-date statement that must not be treated as standalone quarterly EPS. |
| Nasdaq earnings-surprise table | Stops at Q2 2026; 1.75 appears in its September 2025 row. |
| Yahoo financial time series | Stops at Q2 2026, despite current-quarter articles being available. |
| SEC company facts from Pi | HTTP 403; no attempt to evade the restriction. |
| Massive/Benzinga earnings with the existing key | HTTP 403; a separate paid entitlement is required, so it is not enabled. |
| Yahoo public article and RSS | Current, accessible basic EPS of 3.25 USD for Q3 2026. |

The free fallback uses RSS to discover company-specific earnings stories, then
checks at most two public Yahoo articles. It accepts only an explicit **basic or
diluted EPS** line identifying the exact fiscal quarter and year, compared with
the previous year, in a recent article about that company. It checks NewsArticle
metadata and publication time and reads article text, excluding scripts, SVG
graphics and unrelated price widgets. Previews, conflicting values, future
articles, other issuers and prior quarters are rejected. No EPS is calculated
from share counts, EBITDA or net income.

This is **news evidence**, not a replacement consensus API. It shows the basis,
currency and source and leaves the comparable estimate unavailable, with no
beat/miss grade. The VST pipeline was verified against the live RSS and article
using a temporary database before rollout. The source is a public website and
can change or become unavailable; unmatched articles leave a visible gap instead
of a guessed value. No new account, API key or subscription is needed.

Automatic attempts are hourly for recent unresolved/news-evidence releases;
manual refreshes respect the existing shared Yahoo request allowance. Receipt
times, publication times, the fiscal label and source URL stay in comparison
provenance and new snapshots. Existing finalized snapshots remain unchanged.
Correction alerts have durable identities distinct from the withdrawn verdict,
so a previous erroneous EPS claim does not suppress the corrected news figure.

The 0.8.2 default-refresh commands and insider coverage improvements remain.

Pi rollout and the final command walkthrough passed. The running 0.8.3 earnings
modules matched the worktree hashes; VST displayed 3.25 USD basic news EPS.
GOOGL retained 172 unique transactions, zero P purchases, three requests and no
partial-range flag. Both jobs completed and the delivery queue drained without
an error. Focused unit results: 36 earnings, 17 insider, 16 shared-control,
31 bot and 14 snapshot cases, with changed-file Ruff and shell syntax checks.
