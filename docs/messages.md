# Telegram messages through 0.8

0.8.6 adds premarket prices for held/watched stocks plus SPY/QQQ, with previous
close/date, source time and indicative/delay/stale labels. VIX is shown in points
with its dated change baseline and a 15m-delay label. Weekends/early briefings
show previous closes and explicit current-premarket gaps. Market news has up to
three macro/index drivers. Company and market extracts identify article-backed,
feed-provided or headline-only evidence; `/news TICKER` includes these extracts.
See [0.8.6](release-0.8.6.md) for refresh/budget and evidence limits.

From 0.8.5, briefing headers name the saved timezone and the market line shows
local session open/close times, a pre-open countdown and the next verified open
on weekends/holidays. Change the travel timezone with `/set timezone AREA/CITY`.
An automatic HH:MM briefing uses the local date of that open, with one claim per
US session even across timezone edits. See [0.8.5 details](release-0.8.5.md).

Reports use **bold headings, tickers and key labels**, *italic supporting notes*
and monospace commands. Styling is added at delivery, including for existing
queued replies, using Telegram's native
[message entities](https://core.telegram.org/bots/api#messageentity). Text stays
literal, so provider HTML/Markdown cannot turn into formatting. Emoji offsets
use UTF-16 and are calculated separately for each message chunk. Keyboard,
delivery retries and saved state use their existing paths. Styling itself needs
no migration; the 0.8 data migrations are documented in its release notes.

In 0.8, news shows a **company aggregate** with signed bias, distinct story
count, short headlines and age. Bias ranges from −1.0 to +1.0; it is a rules
score rather than confidence or a predicted return. The overview covers up to
ten companies; `/news TICKER` shows up to three distinct contributing events.
For example, using invented data:

**📰 News sentiment**

🟢 **GOOGL** • **Bullish (+0.5)**  
2 distinct stories  
Alphabet raises guidance • 1h ago  
Second view: agrees on direction • 2 matched stories  
Second view uses different text scope.

🔴 **VST** • **Bearish (-0.5)**  
1 distinct story  
Vistra cuts guidance • 2h ago  
Second view: differs • 1 matched stories

⚪ **IBM** • **No clear signal**  
No recent cached news.

*More: /news TICKER*

*Sentiment is heuristic, not a price forecast.*

Opposing events remain marked mixed even if their aggregate is 0.0. Unknown
or insufficient evidence is not Neutral. Vendor comparisons require directly
relevant matched stories; missing assessments show unavailable. Categorical
vendor labels have no invented numeric strength. Brief source/cache/coverage
notices remain visible. Details: [0.8 controls](release-0.8.md).

Default messages omit source links, publisher/provider names, full extracts and
method jargon. Bounded inputs, observed versions, source identities, company
context and separate assessments remain in storage. No custom LLM is installed.

`/snapshot now` returns a compact frozen-input summary, with capture time,
reporting currency, company count, positions/news and data gaps. `/snapshot latest`
reads that same record after later data changes. Automatic capture stays silent;
`/status` and briefing show the saved/scheduled status. Snapshot records are
separate from portfolio progress and database backups.

Earnings messages show the expected date, countdown, release window, fiscal
period, EPS/revenue expectations and a compact checked time. A ticker view also
shows four recent quarters of EPS actual/estimate and HIT/MET/MISS. New comparable
results generate a compact update, with later revenue sent without repeating EPS.
See [earnings behavior and limits](release-0.7.2.md).
Yahoo news-based results show basic/diluted EPS separately from the saved calendar
EPS/revenue forecasts, labelled with the forecast source. These forecasts remain
ungraded context until their basis is matched. The same estimates appear in
briefing cards and future result alerts. See [0.8.4](release-0.8.4.md).
Held exposure is included when relevant;
stale/conflicting or unconfirmed schedules remain explicit. Reports, warnings,
date corrections and briefing entries omit source links. For example:

**📅 Upcoming earnings**

**VST** • 2026-11-06 • in 3 days  
before-open • FY2026 Q3  
*Last checked: 03 Nov 12:00 UTC*

*Dates may change. These are provider-reported schedules; times are approximate.*

Insider reports put the person/role, total value, shares/price and trade/filing
dates together, with an explicit Qualified/Unqualified status. Reports show up to
five recent records; briefing entries omit repeated execution details. Original
SEC links and required Tracefour attribution remain with the insider data.
Purchase qualification and alert deduplication are unchanged. For example:

**🚨 Insider purchases**

**VST** • Jane Doe (director/CEO)  
**Value:** 100,000.00 USD • 1,000 shares × 100.0000 USD  
**Traded:** 2026-11-03 • Filed: 2026-11-03 22:59 UTC  
open-market/private purchase • ✅ Qualified

Filing links and attribution follow each record.

Portfolio, price and alert reports retain their numbers and source/freshness
information, with the same visual hierarchy. For example:

**Portfolio • EUR**

**VST** • 5 shares  
**Cost basis:** 500.00 EUR  
**Unrealized P/L:** +25.00 EUR

These examples demonstrate formatting, not current market data or actual holdings.
Deploy with the existing script, then view `/news VST`, `/briefing now` and
`/portfolio` in the private bot. No live bot poller or message is needed for local
development. Focused fixtures cover styling, emoji/chunk boundaries, persisted
delivery retries and compact news; no broad integration/image/ARM run is required.
