"""Small outbound news adapters; all responses remain bounded and attributed."""

from datetime import UTC, datetime

from trabe.news_quality import direction, finite

INTERVALS = {"finnhub": 10800, "yahoo-rss": 3600, "massive": 3600, "marketaux": 10800}


def array(value):
    return value if isinstance(value, list) else []


def timestamp(value):
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.timestamp() if parsed.tzinfo else None
    except (AttributeError, TypeError, ValueError, OverflowError):
        return None


def vendor_score(provider, ticker, value, *, explanation="", highlights=None):
    if provider == "massive":
        if not isinstance(value, str) or value not in {"positive", "neutral", "negative"}:
            return None
        return {
            "ticker": ticker,
            "direction": value,
            "bias": None,
            "raw_label": value,
            "scale": "categorical",
            "method": "massive-insights/unknown",
            "basis": "vendor_context_unavailable",
            "reason": explanation[:320],
            "highlights": [],
        }
    if not finite(value) or not -1 <= value <= 1:
        return None
    return {
        "ticker": ticker,
        "direction": direction(value),
        "bias": value,
        "raw_label": None,
        "scale": "vendor-signed-sentiment",
        "method": "marketaux-entity/unknown",
        "basis": "vendor_highlights",
        "reason": "Entity-specific highlighted text",
        "highlights": (highlights or [])[:3],
    }


class NewsSources:
    def __init__(self, http, normalize, clean):
        self.http, self.normalize, self.clean = http, normalize, clean

    def fetch(self, provider, ticker, now):
        start = datetime.fromtimestamp(now - 72 * 3600, UTC).isoformat()
        if provider == "massive":
            raw = self.http.news_api(
                "massive",
                ticker=ticker,
                **{
                    "published_utc.gte": start,
                    "sort": "published_utc",
                    "order": "desc",
                    "limit": 100,
                },
            )
            received = max(now, getattr(self.http, "received_at", now))
            rows = raw.get("results")
            if not isinstance(rows, list):
                raise ValueError("Invalid Massive news response.")
            result = []
            for row in rows[:100]:
                if not isinstance(row, dict) or ticker not in array(row.get("tickers")):
                    continue
                annotations = []
                for insight in array(row.get("insights"))[:20]:
                    if not isinstance(insight, dict) or insight.get("ticker") != ticker:
                        continue
                    score = vendor_score(
                        provider,
                        ticker,
                        insight.get("sentiment"),
                        explanation=self.clean(insight.get("sentiment_reasoning"), 320),
                    )
                    if score:
                        annotations.append(score)
                normalized = self.normalize(
                    {
                        "id": row.get("id"),
                        "headline": row.get("title"),
                        "summary": row.get("description", ""),
                        "url": row.get("article_url"),
                        "source": (row.get("publisher") or {}).get("name", provider),
                        "related": ticker,
                        "datetime": timestamp(row.get("published_utc")),
                        "annotations": annotations,
                    },
                    ticker,
                    provider,
                    received,
                )
                if normalized:
                    result.append(normalized)
            return result, bool(raw.get("next_url")) or len(rows) >= 100
        raw = self.http.news_api(
            "marketaux",
            symbols=ticker,
            language="en",
            published_after=start,
            limit=3,
            group_similar="true",
            filter_entities="true",
        )
        received = max(now, getattr(self.http, "received_at", now))
        rows = raw.get("data")
        if not isinstance(rows, list):
            raise ValueError("Invalid Marketaux news response.")
        result = []
        for row in rows[:3]:
            if not isinstance(row, dict):
                continue
            entities = [
                entity
                for entity in array(row.get("entities"))[:20]
                if isinstance(entity, dict) and entity.get("symbol") == ticker
            ]
            if not entities:
                continue
            annotations = []
            for entity in entities:
                highlights = [
                    self.clean(h.get("highlight"), 240)
                    for h in array(entity.get("highlights"))[:3]
                    if isinstance(h, dict)
                ]
                score = vendor_score(
                    provider, ticker, entity.get("sentiment_score"), highlights=highlights
                )
                if score:
                    score["match_score"] = (
                        entity.get("match_score") if finite(entity.get("match_score")) else None
                    )
                    annotations.append(score)
            normalized = self.normalize(
                {
                    "id": row.get("uuid"),
                    "headline": row.get("title"),
                    "summary": row.get("snippet") or row.get("description", ""),
                    "url": row.get("url"),
                    "source": row.get("source", provider),
                    "related": ticker,
                    "datetime": timestamp(row.get("published_at")),
                    "annotations": annotations,
                },
                ticker,
                provider,
                received,
            )
            if normalized:
                result.append(normalized)
        return result, len(rows) >= 3
