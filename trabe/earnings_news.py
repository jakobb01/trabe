"""Conservative current-quarter EPS evidence from public Yahoo news articles."""

import re
from datetime import UTC, datetime
from urllib.parse import urlsplit

from trabe.articles import ArticleCache, parse_document
from trabe.earnings_results import number
from trabe.news_quality import context, mentions
from trabe.signals import SignalStore, WorkStopped


def extract(body, ticker, period, release_date, now, aliases=()):
    """Require an explicit basic/diluted EPS quarter/year comparison, not a price."""
    document = parse_document(body, now)
    return (
        extract_document(document, ticker, period, release_date, now, aliases) if document else None
    )


def extract_document(document, ticker, period, release_date, now, aliases=()):
    match = re.fullmatch(r"FY(\d{4}) Q([1-4])", period)
    if not match or document["received_at"] > now:
        return None
    year, quarter = match.groups()
    metadata = {"headline": document["headline"], "datePublished": document["published_iso"]}
    if (
        not isinstance(metadata.get("headline"), str)
        or not mentions(metadata["headline"], context(ticker, extra=aliases))
        or re.search(
            r"\b(preview|forecasts?|forecasters|expects?|expected|ahead|estimates?)\b",
            metadata["headline"],
            re.I,
        )
    ):
        return None
    try:
        published = datetime.fromisoformat(metadata["datePublished"].replace("Z", "+00:00"))
        if published.tzinfo is None or published.timestamp() > now:
            return None
        release = datetime.fromisoformat(release_date).date()
        if not 0 <= (published.date() - release).days <= 3:
            return None
    except (KeyError, TypeError, ValueError, AttributeError):
        return None
    text = document["text"]
    pattern = (
        r"\b(?P<method>Basic|Diluted)\s+EPS\s*\(\s*Q"
        + quarter
        + r"\s+"
        + year
        + r"\s+vs\.?\s+Q"
        + quarter
        + r"\s+"
        + str(int(year) - 1)
        + r"\s*\)\s*:\s*"
        + r"(?P<currency>US\$|USD\s*|EUR\s*|€)\s*(?P<actual>-?\d+(?:\.\d+)?)\b"
    )
    results = [m.groupdict() for m in re.finditer(pattern, text, re.I)]
    unique = {(r["method"].lower(), r["currency"].upper(), r["actual"]) for r in results}
    if len(unique) != 1:
        return None
    result = results[0]
    actual = number(result["actual"])
    if actual is None:
        return None
    return {
        "actual": actual,
        "estimate": None,
        "source": "yahoo-news",
        "basis": result["method"].lower() + " EPS reported in news",
        "currency": "USD" if result["currency"].strip().upper() in {"US$", "USD"} else "EUR",
        "published_at": published.timestamp(),
        "headline": metadata["headline"][:200],
        "fiscal_period": period,
        "checked_at": now,
        "quality": "news evidence; ungraded",
    }


class EarningsNews:
    def __init__(self, store, http):
        self.store, self.http = store, http
        self.repo = SignalStore(store)

    def refresh(self, symbol, *, now, force=False):
        if not hasattr(self.http, "xml"):
            return
        records = [
            r
            for r in self.repo.rows("earnings", [symbol], limit=100)
            if not r["payload"].get("superseded_by")
            and (
                r["payload"].get("comparisons", {}).get("eps", {}).get("actual") is None
                or r["payload"].get("comparisons", {}).get("eps", {}).get("source") == "yahoo-news"
            )
            and not any(r["payload"].get(k) for k in ("conflict", "source_conflicts"))
            and 0
            <= (
                datetime.fromtimestamp(now, UTC).date()
                - datetime.fromisoformat(r["payload"]["date"]).date()
            ).days
            <= 7
        ]
        if not records or (
            not force and not self.repo.due("earnings", "yahoo-news", symbol, 3600, now)
        ):
            return
        from trabe.news import News

        self.repo.attempt("earnings", "yahoo-news", symbol, now)
        try:
            news = News(self.store, self.http)
            company = news.company_context(symbol, now)
            items = news._rss(symbol, now)
            now = news._receipt(now)
            candidates = [
                i
                for i in items
                if mentions(i["headline"], company)
                and re.search(r"earnings|\bQ[1-4]\b|quarter", i["headline"], re.I)
            ]
            failed = False
            cache = ArticleCache(self.store, self.http)
            for item in sorted(candidates, key=lambda i: i["published_at"], reverse=True)[:2]:
                url = item["url"]
                if urlsplit(url).hostname != "finance.yahoo.com":
                    continue
                try:
                    document = cache.get(url, now)
                except WorkStopped:
                    raise
                except Exception:
                    failed = True
                    continue
                received = max(now, getattr(self.http, "received_at", now))
                if not document:
                    continue
                for row in records:
                    payload = row["payload"]
                    evidence = extract_document(
                        document,
                        symbol,
                        payload["period"],
                        payload["date"],
                        received,
                        company["aliases"],
                    )
                    if not evidence:
                        continue
                    evidence["source_url"] = url
                    evidence["article_received_at"] = document["received_at"]
                    payload.setdefault("comparisons", {})["eps"] = evidence
                    payload.setdefault("result_seen", {}).setdefault("eps", received)
                    payload["reported"] = True
                    self.repo.put(
                        "earnings",
                        row["key"],
                        symbol,
                        row["source"],
                        payload,
                        now=received,
                        event_time=row["event_time"],
                        available_at=row["available_at"],
                    )
                    self.repo.ok("earnings", "yahoo-news", symbol, received)
                    return
            if failed:
                self.repo.fail(
                    "earnings",
                    "yahoo-news",
                    symbol,
                    "Some Yahoo earnings articles unavailable; cached figures retained",
                    now,
                    retry=3600,
                )
            else:
                self.repo.ok(
                    "earnings",
                    "yahoo-news",
                    symbol,
                    max(now, getattr(self.http, "received_at", now)),
                )
        except WorkStopped:
            raise
        except Exception:
            self.repo.fail(
                "earnings",
                "yahoo-news",
                symbol,
                "Yahoo earnings news unavailable; no figures inferred",
                now,
                retry=3600,
            )
