# Free market context and broader news — 2026-11-08

The owner selected **SPY and QQQ** alongside held/watched stocks and requires free
sources only. Bounded checks ran on the Pi with the normal app client. Evidence:
`.local/market-context-probe/access-pi.json` and `news-access-pi.json`.

| Source checked | Actual result | Decision |
| --- | --- | --- |
| Finnhub VST quote | Valid regular-session observation/previous close; no separate premarket field | Keep existing quote monitoring separate from the new indicative context. |
| Finnhub `^VIX` quote | Unusable quote | Do not call this a verified index feed. |
| Massive stock/index snapshots | Both returned 403 with the existing key | News access does not establish these entitlements; no upgrade enabled. |
| Yahoo public chart, VST/SPY | Valid 1m charts, pre/post timestamps and actual recent premarket bars | Use bounded keyless requests with explicit session/age/delay labels. |
| Yahoo public chart, `^VIX` | Correct INDEX identity, 25.64 latest level, 28.18 previous index close | Display in points, 15m delayed; keep prior-session observations distinct. |
| Cboe daily VIX history | Last daily close 2026-11-06 = 25.64; 2026-11-05 = 28.18 | Independent value/baseline check. CSV is research evidence, not a runtime live-VIX source. |
| Finnhub general news | 100 entries with publication times, URLs, headlines and snippets | Filter actual macro/index drivers; do not score unrelated general headlines as company sentiment. |
| Yahoo index RSS `^GSPC,^IXIC` | 20 entries, including promotional company/opinion stories | An available fallback that still needs filtering; index tags do not guarantee broad-market relevance. |

The source check was on Sunday. It verifies actual recent premarket fields and
the correct last-session VIX value, not a live Monday premarket update. Weekends
and early briefings must show previous closes/current gaps; fixtures cover the
active premarket window and session transitions. QQQ uses the same adapter and
is included in the release's normal briefing refresh/walkthrough.

Yahoo's [official exchange/delay table](https://help.yahoo.com/kb/finance/article-exchanges-data-delays-sln2310.html)
lists Cboe indices as 15m delayed. Its
[quote help](https://help.yahoo.com/kb/SLN2321.html) distinguishes exchange coverage
and extended-hours availability. The observed chart interface is public/keyless,
not a promised supported API/SLA. Equities are labelled indicative minute bars
with unverified feed delay; failed or changed responses retain labelled cache.

Massive's [extended-hours documentation](https://massive.com/knowledge-base/article/market-data-outside-of-normal-hours)
explains session coverage while making recency depend on the tier. The observed
403s take precedence over assuming that the existing news key covers snapshots.
Cboe explicitly offers [daily VIX downloads](https://www.cboe.com/tradable_products/vix/vix_historical_data);
daily dates are not intraday observation timestamps. Finnhub's
[official API schema](https://raw.githubusercontent.com/Finnhub-Stock-API/finnhub-go/master/api/openapi.yaml)
documents `/news?category=general` as market news.

No new API key, paid plan, alternate-host bypass or heavy model is required.
