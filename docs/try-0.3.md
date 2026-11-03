# Try trabe 0.3 in Telegram

Use the private conversation with your paired bot. Your existing watchlist and
preferences survive the upgrade. Monitoring starts off until you enable it.

## Start the feed

Send these commands individually:

```text
/menu
/provider finnhub
/monitor on
/status
/quote VST
/prices
```

`/provider finnhub` selects the verified free account. If verification is queued,
wait for its result before enabling monitoring. `/status` shows the US session,
connection, subscriptions, symbol issues and most recent source observation.
While the market is open, supported watchlist stocks should become subscribed
and receive fresh observations. Outside the core session, a closed state is
expected; monitoring resumes at the next verified opening.

`/quote VST` names Finnhub, displays USD, source UTC time, age and freshness.
`/prices` reads cached observations without spending provider requests. Free feed
coverage can differ from a consolidated market screen. A quiet stock may become
stale and temporarily ineligible for alerts.

## Check and adjust rules

```text
/alerts
/alert VST -5
/alerts
/alert VST default
```

The example temporarily gives VST a -5% rule, then restores the global threshold.
Use `/alert VST off` to keep collecting VST while disabling its proactive alerts.
Alerts require a fresh qualifying drop against a compatible previous close; a
quiet session may produce none. `/test` checks Telegram delivery.

At most one alert is created per stock/rule/exchange session. Changing thresholds
or removing/re-adding a symbol does not rearm an event already claimed that day.
Large opening gaps can suppress alerts for baseline/corporate-action review;
inspect the symbol issue shown by `/status`.

## Pause, resume and inspect storage

```text
/pause 10m
/status
/quote VST
/storage
/resume
```

Pause stops automation, while manual commands and retention remain available.
The pause persists through restarts. A request already in flight may finish.
Resume clears the pause and uses fresh observations without replaying missed
alerts. `/monitor off` disables collection until `/monitor on` is sent again.

```text
/history VST 5
/storage
```

Daily summaries appear after the actual session close plus ten minutes. Before
the first finalized session, an empty history is expected. Summaries show observed
first/high/low/last prices, provider and coverage; a late start is marked partial.
Minute detail is removed after rollup. Daily history and automatic backups expire
after three calendar months; automatic backups also have a count limit.

`/storage` shows minute/daily rows, database/WAL/backup sizes, retention cutoff and
maintenance health. It makes no market requests. The old weekday summary schedule
keeps its saved setting independently of intraday monitoring.
