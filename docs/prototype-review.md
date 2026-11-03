# Prototype assessment

The initial prototype was reviewed on 2026-11-02. It contained 16 Python,
configuration and documentation files, including an environment file with
placeholder credentials. The original archive is retained outside the source
tree; its broken source is not copied into the runnable package.

## Findings

* Four modules fail Python compilation: `telegram_bot.py`, `market_radar.py`,
  `insider_tracker.py` and `earnings_watchdog.py`. Several f-strings contain literal
  line breaks inside single-quoted strings.
* Telegram commands have no owner authorization. Anyone who finds the bot could
  mutate its watchlist and cause paid or rate-limited API work.
* The supplied systemd unit starts `main.py`; the separate interactive `bot.py`
  process is never started by that service.
* Market-hours checks use the Pi's local hour, ignore New York timezone/DST and
  holidays, use an imprecise opening time, and do not gate the radar cycle.
* The news scheduler queries each seeded symbol every 30 minutes. That exceeds a
  normal free Alpha Vantage daily allowance with even a small watchlist.
* Insider fetching is a placeholder. The briefing lists symbols and generic
  statuses but does not actually aggregate and summarize their news.
* Removing every symbol causes the default watchlist to reappear on initialization.
* Environment settings are import-time constants, so they are not configurable
  through Telegram. Directory creation also happens as an import side effect.
* Unescaped article text is interpolated into HTML messages. Error logging can
  include credential-bearing request URLs. Log files lack the rotation promised
  by the source comment.
* Dependencies are open-ended lower bounds, with no tests, container or rollback.

## Language decision

Keep Python. This service primarily waits for Telegram and financial APIs and
stores a small amount of SQLite state. The workload does not call for Rust's CPU
performance or a low-level memory model. Python also has a broad financial-data
ecosystem for later phases.

The initial production scaffold is deliberately small and uses the Python standard
library. It replaces the broken runtime and security/configuration boundaries,
while preserving the original Telegram-first product direction. Future financial
data and AI integrations can be introduced behind the existing API and storage
modules with explicit dependencies and tests. A Rust rewrite is an option if a
measured resource, deployment or performance problem later justifies it.

## Next phases

1. Decide required quote freshness, watchlist size and data-provider budget.
2. Implement daily/weekly dip calculations with timestamped data, exchange-aware
   scheduling and durable alert deduplication.
3. Add real earnings and insider sources; verify transaction types and dates.
4. Add a real RSS/news digest and optional AI summarization with source links.
5. Extend Telegram preferences for each implemented feature, including pause,
   schedules and alert thresholds. Do not expose settings with no effect.

The working test deployment must come before these features. There is no order
execution or brokerage connection in either the current scope or deployment.
