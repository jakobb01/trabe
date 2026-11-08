"""A small, source-qualified macro news view, separate from company sentiment."""

import json
import re

from trabe.articles import ArticleCache, sentences
from trabe.news import News
from trabe.signals import SignalStore, WorkStopped

WINDOW = 72 * 3600
TOPICS = re.compile(
    r"\b(?:wall street|s&p\s*500|nasdaq|dow jones|stock futures|equity futures|"
    r"federal reserve|fed|interest rates?|inflation|consumer prices?|cpi|ppi|"
    r"payrolls|jobs report|unemployment|gdp|treasury (?:yields?|bonds?|notes?|auctions?)|treasuries|bond yields?|"
    r"oil prices?|crude oil|tariffs?|trade war|government shutdown|us economy|u\.s\. economy)\b",
    re.I,
)
OPINION = re.compile(
    r"\b(?:prediction|best.performing|stocks? to buy|why i['’]m|billionaires?|"
    r"warren buffett|my portfolio|retirement|top \d+|1 thing investors)\b",
    re.I,
)


def relevant(item):
    return bool(
        TOPICS.search(item["headline"] + " " + item.get("snippet", ""))
    ) and not OPINION.search(item["headline"])


class MarketNews:
    def __init__(self, store, http):
        self.store, self.http = store, http
        self.repo = SignalStore(store)
        self.news = News(store, http)

    def refresh(self, *, now, force=False):
        old = self.repo.get("market-news", "latest")
        if old and not force and 0 <= now - old["last_seen"] < 3600:
            return
        selected = self.store.settings()["news_sources"].split(",")
        for provider in (
            p for p in ("finnhub", "yahoo-rss") if ("yahoo" if p == "yahoo-rss" else p) in selected
        ):
            if not force and not self.repo.due("market-news", provider, "*", 3600, now):
                continue
            self.repo.attempt("market-news", provider, "*", now)
            try:
                if provider == "finnhub":
                    raw = self.http.finnhub("news", category="general")
                    received = self.news._receipt(now)
                    if not isinstance(raw, list):
                        raise ValueError("Invalid market news response.")
                    items = [
                        item
                        for row in raw[:100]
                        if isinstance(row, dict)
                        and (
                            item := self.news._normalize(
                                row | {"related": None}, "", provider, received
                            )
                        )
                    ]
                else:
                    items = self.news._rss("^GSPC,^IXIC", now)
                    received = self.news._receipt(now)
                items = [
                    item
                    for item in items
                    if relevant(item) and 0 <= received - item["published_at"] <= WINDOW
                ]
                self.repo.ok("market-news", provider, "*", received)
                if not items:
                    continue
                unique, urls, headlines = [], set(), set()
                cache = ArticleCache(self.store, self.http)
                for item in sorted(items, key=lambda i: i["published_at"], reverse=True):
                    headline = re.sub(
                        r"\s+(?:-|\|)\s*" + re.escape(item["publisher"]) + r"\s*$",
                        "",
                        item["headline"],
                        flags=re.I,
                    )
                    signature = re.sub(r"\W+", " ", headline.casefold()).strip()
                    if item["url"] in urls or signature in headlines:
                        continue
                    urls.add(item["url"])
                    headlines.add(signature)
                    try:
                        document = (
                            cache.get(item["url"], self.news._receipt(received))
                            if "yahoo" in selected
                            else None
                        )
                    except WorkStopped:
                        raise
                    except Exception:
                        document = None
                    if document and relevant({"headline": document["headline"], "snippet": ""}):
                        snippet = sentences(document, maximum=480)
                        if snippet:
                            item = item | {
                                "snippet": snippet,
                                "input_quality": "article",
                                "published_at": document["published_at"],
                            }
                    unique.append(
                        {
                            k: item[k]
                            for k in (
                                "headline",
                                "snippet",
                                "url",
                                "published_at",
                                "input_quality",
                                "provider",
                                "publisher",
                            )
                        }
                    )
                    unique[-1]["headline"] = unique[-1]["headline"][:200]
                    unique[-1]["snippet"] = unique[-1]["snippet"][:480]
                    if len(unique) == 10:
                        break
                checked = self.news._receipt(received)
                payload = {
                    "items": unique,
                    "source": provider,
                    "received_at": checked,
                    "feed_received_at": received,
                    "coverage": "selected broad-market stories; not complete coverage",
                }
                while len(json.dumps(payload).encode()) > 30000 and payload["items"]:
                    payload["items"].pop()
                self.repo.put(
                    "market-news", "latest", "", provider, payload, now=checked, event_time=checked
                )
                return
            except WorkStopped:
                raise
            except Exception:
                self.repo.fail(
                    "market-news",
                    provider,
                    "*",
                    "Market news unavailable or deferred; saved stories retained.",
                    now,
                )

    def briefing(self, *, now):
        row = self.repo.get("market-news", "latest")
        lines = ["Market news • selected broad-market drivers"]
        items = row["payload"]["items"] if row and row["last_seen"] <= now else []
        current = [
            item for item in items if 0 <= now - item["published_at"] <= WINDOW and relevant(item)
        ]
        for item in current[:3]:
            label = (
                "Article extract"
                if item["input_quality"] == "article"
                else "Feed extract"
                if item["snippet"]
                else "Headline only"
            )
            lines.append(f"• {item['headline']} • {News._age(item['published_at'], now)} ago")
            if item["snippet"]:
                lines.append(f"{label}: {item['snippet'][:240]}")
            else:
                lines.append(label + ".")
        if not current:
            lines.append("No recent qualifying market stories cached; coverage may be unavailable.")
        if row and row["payload"]["source"] == "yahoo-rss":
            lines.append("Yahoo RSS fallback • selected macro/index headlines.")
        if row and now - row["last_seen"] > 3 * 3600:
            lines.append("Market news cache is stale.")
        if any(s["error"] for s in self.repo.status("market-news")):
            lines.append("Some market news requests failed/deferred; available stories retained.")
        return lines
