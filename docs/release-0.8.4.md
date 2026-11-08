# 0.8.4 — show calendar estimates beside Yahoo earnings evidence

Yahoo result cards hid the existing calendar forecasts: the news evidence stores
no comparable estimate, and the result renderer displayed only that evidence.
The values were still present in the earnings record. The Pi's VST record had
EPS estimate 2.991 and revenue estimate 6084287722 while the news-reported basic
EPS was 3.25 USD.

Reported-result cards now include a separate **Calendar estimates (Finnhub)**
line with EPS and revenue expectations. The same line appears in briefing cards
and future news-based result/correction alerts. Unknown-source forecasts are
labelled saved calendar estimates; missing values remain unavailable and zero
estimates remain valid. The empty revenue comparison now says that the actual
is unavailable, rather than implying that its saved estimate is missing.

The calendar forecasts are context. They are not relabelled as Yahoo consensus,
copied into the article's paired-estimate field or used to grade news-reported
basic EPS against an unverified basis. Actual figures, source provenance, alert
identities and stored snapshots are unchanged. Schema remains 7.

The public-article reader currently serves the narrow earnings fallback. Reusing
it for fuller news/briefing extracts is a separate roadmap improvement; this
patch immediately improves the earnings section of briefings.

Verified with 37 earnings and 16 shared-control unit cases plus changed-file
Ruff checks. Pi deployment passed readiness, delivery and restart/persistence
checks. A cached VST report was delivered with the forecast line above; its
briefing card also contained that line, and both changed source hashes matched
the worktree. No new API request was needed for the cached-report walkthrough.
