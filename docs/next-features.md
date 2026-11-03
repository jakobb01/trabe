# Product direction after deployment

The implemented additions keep Telegram as the entire user interface. Cached
prices let the user inspect the watchlist without spending requests; the scan
worker keeps commands responsive; `/lastscan` recovers a missed result; and a
weekday summary can be enabled without visiting a website.

The next feature decision is data freshness. The free end-of-day provider proves
the deployment plumbing, but cannot support reliable intraday dip alerts. Pick the
required exchange coverage, latency and daily request budget before building an
intraday scheduler. Keep the provider boundary independent of Telegram commands.

Useful follow-on work, in priority order:

1. Timestamped daily/weekly dip rules and per-ticker alert preferences. Deduplicate
   by symbol, rule and source trading date, with a durable cooldown after delivery.
2. Telegram configuration buttons for common schedules/thresholds, a snooze command
   and a configuration export/restore flow. Credentials should remain outside chat.
3. An earnings calendar with source dates and explicit uncertainty, then real
   open-market insider transactions filtered by role and transaction type.
4. A source-linked morning digest that shares the API budget with price checks.
   Add optional AI summaries after reliable article collection and URL deduplication.

Treat a news sentiment label and a price dip as observations, not recommendations
or proof of future price movement. Trading remains manual; no brokerage integration
or order execution is included in the project scope.
