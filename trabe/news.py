"""Bounded source-linked news extracts and transparent local sentiment signals."""

import hashlib
import html
import json
import math
import re
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from xml.etree import ElementTree

from trabe.ledger import Ledger
from trabe.signals import SignalStore, WorkStopped

WINDOW = 7 * 86400
PER_SYMBOL = 20
GLOBAL_LIMIT = 1000
METHOD = "lexicon-v1"
TRACKING = {"fbclid", "gclid", "guccounter", "guce_referrer", "guce_referrer_sig"}
POSITIVE = (
    r"beat(?:s)? (?:earnings |analyst )?(?:estimates|expectations)",
    r"(?:raise[sd]?|raising) (?:its )?(?:guidance|forecast|outlook)",
    r"(?:record|rising|higher|strong) (?:profit|revenue|sales|earnings|demand)",
    r"(?:profit|revenue|sales|earnings) (?:growth|rise[sd]?|increase[sd]?)",
    r"upgrad(?:e[sd]?|ing)",
    r"buyback[ s]?",
    r"(?:win[ s]?|receiv(?:e[sd]?|ing)) (?:regulatory )?approval",
    r"(?:surge[sd]?|rall(?:y|ies|ied)|bullish)",
)
NEGATIVE = (
    r"miss(?:es)? (?:earnings |analyst )?(?:estimates|expectations)",
    r"(?:cut[ s]?|lower[sd]?|lowering) (?:its )?(?:guidance|forecast|outlook)",
    r"(?:falling|lower|weak) (?:profit|revenue|sales|earnings|demand)",
    r"(?:profit|revenue|sales|earnings) (?:decline[sd]?|fall[ s]?|drop[ s]?)",
    r"downgrad(?:e[sd]?|ing)",
    r"(?:lawsuit|investigation|recall|bankruptcy|default|layoffs)",
    r"(?:regulatory )?rejection",
    r"(?:slump[ s]?|plunge[sd]?|bearish)",
)


def text(value, limit=4096):
    if not isinstance(value, str):
        return ""
    value = html.unescape(value[:limit])
    value = re.sub(r"<(script|style)\b[^>]*>.*?</\1>", " ", value, flags=re.I | re.S)
    value = re.sub(r"<[^>]*>", " ", value)
    return " ".join(value.split())


def canonical_url(value):
    """Remove tracking only; keep query parameters that identify an article."""
    if not isinstance(value, str) or not value or len(value) > 2000:
        return None
    try:
        parts = urlsplit(value)
        if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
            return None
        if parts.username or parts.password or any(c.isspace() for c in value):
            return None
        host = parts.hostname.lower()
        port = parts.port
        authority = ("[" + host + "]" if ":" in host else host) + (
            f":{port}" if port and port != (443 if parts.scheme.lower() == "https" else 80) else ""
        )
        parameters = parse_qsl(parts.query, keep_blank_values=True)
        if any(
            key.lower() in {"token", "apikey", "api_key", "access_token", "secret"}
            for key, _ in parameters
        ):
            return None
        query = urlencode(
            sorted(
                (key, val)
                for key, val in parameters
                if not key.lower().startswith("utm_") and key.lower() not in TRACKING
            )
        )
        return urlunsplit((parts.scheme.lower(), authority, parts.path or "/", query, ""))
    except (ValueError, UnicodeError):
        return None


def sentiment(value):
    evidence = []
    lower = value.lower()
    for patterns, sign in ((POSITIVE, 1), (NEGATIVE, -1)):
        for pattern in patterns:
            for match in re.finditer(r"\b(?:" + pattern + r")\b", lower):
                before = re.findall(r"[a-z']+", lower[max(0, match.start() - 35) : match.start()])
                negated = any(word in {"not", "no", "never", "without"} for word in before[-3:])
                evidence.append(-sign if negated else sign)
    score = round(sum(evidence) / max(2, len(evidence)), 3)
    label = "Bullish" if score > 0.15 else "Bearish" if score < -0.15 else "Neutral"
    return label, score


def _sentence(value, limit=235):
    value = value.strip()
    if len(value) > limit:
        value = value[: limit - 1].rsplit(" ", 1)[0] + "…"
    return value if value.endswith((".", "!", "?", "…")) else value + "."


def summarize(headline, summary):
    """Two short extracts, never invented article content or a price prediction."""
    headline, snippet = text(headline, 512), text(summary)
    pieces = [s.strip() for s in re.split(r"(?<=[.!?])\s+(?=[A-Z0-9\"“])", snippet) if s.strip()]
    if pieces and pieces[0].rstrip(".!?").casefold() == headline.rstrip(".!?").casefold():
        pieces = pieces[1:]
    if snippet and pieces:
        chosen = pieces[:2]
        if len(chosen) == 1:
            chosen.append("See the linked source for the rest of the report")
        kind = "provider-extract"
    else:
        chosen = [headline, "Only the headline was supplied by this feed"]
        kind = "headline-only"
    label, score = sentiment(headline + " " + snippet)
    return {
        "summary": " ".join(_sentence(value) for value in chosen),
        "summary_kind": kind,
        "summary_method": "extractive-v1",
        "sentiment": label,
        "score": score,
        "sentiment_method": METHOD,
    }


class News:
    def __init__(self, store, http):
        self.store, self.http = store, http
        self.repo = SignalStore(store)

    def _receipt(self, now):
        received = getattr(self.http, "received_at", now)
        if type(received) not in (int, float) or not math.isfinite(received):
            return now
        return max(now, received)

    @staticmethod
    def _timestamp(value, now):
        if type(value) not in (int, float) or not math.isfinite(value):
            return None
        if not now - WINDOW <= value <= now + 2:
            return None
        return float(value)

    def _normalize(self, raw, symbol, source, now):
        headline = text(raw.get("headline"), 512)
        url = canonical_url(raw.get("url"))
        published = self._timestamp(raw.get("datetime"), now)
        related = raw.get("related")
        if related and isinstance(related, str):
            tickers = set(re.split(r"[,;\s]+", related.upper()))
            if symbol not in tickers:
                return None
        if not headline or not url or published is None:
            return None
        provider_id = raw.get("id")
        provider_id = str(provider_id)[:150] if type(provider_id) in (str, int) else ""
        return {
            "headline": headline[:200],
            "url": url,
            "canonical_url": url,
            "publisher": text(raw.get("source"), 160) or source,
            "published_at": published,
            "first_seen_at": now,
            "last_seen_at": now,
            "content_asof": now,
            "summary_provider": source,
            "symbols": [symbol],
            "provider_ids": {source: [provider_id]} if provider_id else {},
            **summarize(headline, raw.get("summary", "")),
        }

    def _rss(self, symbol, now):
        url = "https://feeds.finance.yahoo.com/rss/2.0/headline?" + urlencode(
            {"s": symbol, "region": "US", "lang": "en-US"}
        )
        body = self.http.xml(url, provider="yahoo")
        now = self._receipt(now)
        if isinstance(body, str):
            body = body.encode()
        if not isinstance(body, bytes) or len(body) > 2_000_000:
            raise ValueError("Invalid Yahoo feed")
        if b"<!DOCTYPE" in body.upper() or b"<!ENTITY" in body.upper():
            raise ValueError("Unsupported XML declarations")
        root = ElementTree.fromstring(body)
        if root.tag.rsplit("}", 1)[-1] != "rss":
            raise ValueError("Unexpected Yahoo feed")
        items = []
        for item in root.findall(".//item")[:100]:
            try:
                published = parsedate_to_datetime(item.findtext("pubDate", ""))
                if published.tzinfo is None:
                    raise ValueError
                stamp = published.timestamp()
            except (TypeError, ValueError, OverflowError):
                continue
            raw = {
                "id": item.findtext("guid", ""),
                "headline": item.findtext("title", ""),
                "summary": item.findtext("description", ""),
                "url": item.findtext("link", ""),
                "datetime": stamp,
                "source": "Yahoo Finance RSS",
            }
            normalized = self._normalize(raw, symbol, "yahoo-rss", now)
            if normalized:
                items.append(normalized)
        return items

    def _records(self, symbols=None, now=None, limit=None):
        rows = self.repo.rows("news", limit=GLOBAL_LIMIT if limit is None else limit, since=0)
        wanted = set(symbols) if symbols is not None else None
        result = []
        for row in rows:
            payload = row["payload"]
            if wanted is not None and not wanted.intersection(
                payload.get("symbols", [row["symbol"]])
            ):
                continue
            if now is not None and (
                row["available_at"] > now
                or payload.get("content_asof", row["observed_at"]) > now
                or payload["published_at"] < now - 90 * 86400
            ):
                continue
            result.append(row)
        return sorted(result, key=lambda row: row["payload"]["published_at"], reverse=True)

    def _merge(self, items, symbol, source, now):
        records = self._records()
        urls = {row["payload"].get("canonical_url"): row for row in records}
        identities = {
            (provider, identity): row
            for row in records
            for provider, ids in row["payload"].get("provider_ids", {}).items()
            for identity in ids
        }
        for item in sorted(items, key=lambda entry: entry["published_at"], reverse=True)[
            :PER_SYMBOL
        ]:
            prior = urls.get(item["canonical_url"])
            if prior is None:
                prior = next(
                    (
                        identities[(provider, identity)]
                        for provider, ids in item["provider_ids"].items()
                        for identity in ids
                        if (provider, identity) in identities
                    ),
                    None,
                )
            key = (
                prior["key"]
                if prior
                else hashlib.sha256(item["canonical_url"].encode()).hexdigest()
            )
            old = prior["payload"] if prior else {}
            item["first_seen_at"] = old.get("first_seen_at", now)
            item["symbols"] = sorted(set(old.get("symbols", [])) | {symbol})
            item["provider_ids"] = {
                provider: sorted(set(old.get("provider_ids", {}).get(provider, [])) | set(ids))
                for provider, ids in (old.get("provider_ids", {}) | item["provider_ids"]).items()
            }
            unchanged = all(
                old.get(field) == item[field] for field in ("headline", "summary", "url")
            )
            item["content_asof"] = old.get("content_asof", now) if unchanged else now
            # Prefer a provider snippet over a later headline-only RSS version.
            if (
                old.get("summary_kind") == "provider-extract"
                and item["summary_kind"] == "headline-only"
            ):
                for field in (
                    "headline",
                    "summary",
                    "summary_kind",
                    "summary_method",
                    "sentiment",
                    "score",
                    "sentiment_method",
                    "content_asof",
                    "summary_provider",
                    "publisher",
                ):
                    item[field] = old[field]
            available = max(item["first_seen_at"], item["published_at"])
            self.repo.put(
                "news",
                key,
                item["symbols"][0],
                source,
                item,
                now=now,
                event_time=item["published_at"],
                available_at=available,
            )
            new_row = self.repo.get("news", key)
            urls[item["canonical_url"]] = new_row
            for provider, ids in item["provider_ids"].items():
                for identity in ids:
                    identities[(provider, identity)] = new_row
        self._trim(now)
        self._daily(symbol, now)

    def _trim(self, now):
        counts = {}
        for index, row in enumerate(self._records(limit=10000)):
            item = row["payload"]
            keep = []
            if index < GLOBAL_LIMIT and item["published_at"] >= now - 90 * 86400:
                for symbol in item["symbols"]:
                    if counts.get(symbol, 0) < PER_SYMBOL:
                        keep.append(symbol)
                        counts[symbol] = counts.get(symbol, 0) + 1
            if not keep:
                self.repo.delete("news", row["key"])
            elif keep != item["symbols"]:
                item["symbols"] = keep
                self.repo.put(
                    "news",
                    row["key"],
                    keep[0],
                    row["source"],
                    item,
                    now=now,
                    event_time=row["event_time"],
                    available_at=row["available_at"],
                )

    def _daily(self, symbol, now):
        day = datetime.fromtimestamp(now, UTC).date().isoformat()
        key = symbol + ":" + day
        if self.repo.get("news_daily", key):
            return
        rows = [row for row in self._records([symbol], now) if row["event_time"] >= now - 86400]
        counts = {
            label: sum(row["payload"]["sentiment"] == label for row in rows)
            for label in ("Bullish", "Bearish", "Neutral")
        }
        payload = {
            "as_of": now,
            "window_seconds": 86400,
            "coverage": "bounded collected items",
            "sample_limit_per_symbol": PER_SYMBOL,
            "article_count": len(rows),
            "counts": counts,
            "score": round(sum(row["payload"]["score"] for row in rows) / len(rows), 3)
            if rows
            else 0,
            "method": METHOD,
            "references": [
                {
                    "key": row["key"],
                    "url": row["payload"]["url"],
                    "published_at": row["event_time"],
                    "available_at": row["available_at"],
                }
                for row in rows[:3]
            ],
        }
        self.repo.put(
            "news_daily", key, symbol, "derived", payload, now=now, event_time=now, available_at=now
        )

    def _health(self, symbol):
        try:
            return json.loads(self.store.state("news_health_" + symbol, "{}"))
        except (ValueError, TypeError):
            return {}

    def refresh(self, symbols, *, now, force=False):
        result = {"refreshed": 0, "failed": 0, "items": 0}
        for symbol in dict.fromkeys(symbols):
            health = self._health(symbol)
            primary_due = force or self.repo.due("news", "finnhub", symbol, 10800, now)
            fallback_due = health.get("source") == "yahoo-rss" and self.repo.due(
                "news", "yahoo-rss", symbol, 3600, now
            )
            if not primary_due and not fallback_due:
                continue
            items, source = None, "finnhub"
            primary_error = health.get("primary_error", "")
            if primary_due:
                self.repo.attempt("news", source, symbol, now)
                try:
                    start = datetime.fromtimestamp(now - WINDOW, UTC).date().isoformat()
                    end = datetime.fromtimestamp(now, UTC).date().isoformat()
                    raw = self.http.finnhub(
                        "company-news", symbol=symbol, **{"from": start, "to": end}
                    )
                    now = self._receipt(now)
                    if not isinstance(raw, list):
                        raise ValueError("Invalid Finnhub news response")
                    items = [
                        item
                        for row in raw[:2000]
                        if isinstance(row, dict)
                        and (item := self._normalize(row, symbol, source, now))
                    ]
                    self.repo.ok("news", source, symbol, now)
                    primary_error = ""
                except WorkStopped:
                    raise
                except Exception as error:
                    now = self._receipt(now)
                    primary_error = (
                        f"Finnhub unavailable (HTTP {getattr(error, 'code', 0) or 'network/data'})"
                    )
                    self.repo.fail("news", source, symbol, primary_error, now, retry=3600)
            if items is None:
                source = "yahoo-rss"
                if not force and not self.repo.due("news", source, symbol, 3600, now):
                    self.store.set_state(
                        "news_health_" + symbol,
                        json.dumps(
                            health
                            | {
                                "checked_at": now,
                                "primary_error": primary_error,
                                "error": "RSS fallback cooling down; cached news retained",
                            }
                        ),
                    )
                    result["failed"] += 1
                    continue
                self.repo.attempt("news", source, symbol, now)
                try:
                    items = self._rss(symbol, now)
                    now = self._receipt(now)
                    self.repo.ok("news", source, symbol, now)
                except WorkStopped:
                    raise
                except Exception:
                    now = self._receipt(now)
                    error = "Yahoo RSS unavailable; previous cached news retained"
                    self.repo.fail("news", source, symbol, error, now, retry=3600)
                    self.store.set_state(
                        "news_health_" + symbol,
                        json.dumps(
                            health
                            | {"checked_at": now, "primary_error": primary_error, "error": error}
                        ),
                    )
                    result["failed"] += 1
                    continue
            self._merge(items, symbol, source, now)
            self.store.set_state(
                "news_health_" + symbol,
                json.dumps(
                    {
                        "source": source,
                        "checked_at": now,
                        "last_success": now,
                        "primary_error": primary_error,
                        "error": "",
                        "items": len(items),
                    }
                ),
            )
            result["refreshed"] += 1
            result["items"] += min(PER_SYMBOL, len(items))
        return result

    def report(self, symbols, *, now):
        symbols = list(dict.fromkeys(symbols))
        if not symbols:
            return "News: no held or watched symbols."
        rows = self._records(symbols, now)
        lines = ["News • cached source extracts • heuristic sentiment (not a forecast)"]
        for symbol in symbols[:10]:
            health = self._health(symbol)
            checked = health.get("last_success")
            age = f"{max(0, int(now - checked)) // 60}m ago" if checked else "never"
            lines.append(
                f"{symbol}: {health.get('source', 'not fetched')} • checked {age}"
                + (" • stale cache" if checked and now - checked > 10800 else "")
            )
            for error in (health.get("primary_error"), health.get("error")):
                if error:
                    lines.append("  " + error)
        if len(symbols) > 10:
            lines.append(
                f"Plus {len(symbols) - 10} other symbols; use /news TICKER for their source status."
            )
        if not rows:
            lines.append("No retained eligible articles. Refresh source data to check coverage.")
        for row in rows[:5]:
            item = row["payload"]
            stamp = datetime.fromtimestamp(item["published_at"], UTC).isoformat()
            lines.extend(
                [
                    "",
                    f"{', '.join(item['symbols'])} • {item['headline']}",
                    f"{item['summary']} [{item['summary_kind']}; {item['summary_method']}]",
                    f"{item['sentiment']} ({item['score']:+.2f}; heuristic {item['sentiment_method']}) • {item['publisher']} via {row['source']} • summary from {item.get('summary_provider', row['source'])} • {stamp}"
                    + (" • older news" if now - item["published_at"] > WINDOW else ""),
                    item["url"],
                ]
            )
        return "\n".join(lines)

    def alerts(self, symbols, *, now):
        return []

    def briefing(self, symbols, *, now, limit=3):
        rows = self._records(symbols, now)
        if not rows:
            return ["News: no eligible cached articles; source coverage may be unavailable."]
        held = Ledger(self.store).held()
        rows.sort(
            key=lambda row: (bool(held.intersection(row["payload"]["symbols"])), row["event_time"]),
            reverse=True,
        )
        lines = []
        for row in rows[: max(1, min(limit, 5))]:
            item = row["payload"]
            stamp = datetime.fromtimestamp(item["published_at"], UTC).strftime("%m-%d %H:%M UTC")
            lines.append(
                f"{', '.join(item['symbols'])}: {item['summary']} • {item['sentiment']} (heuristic {METHOD}) • {item['publisher']} via {row['source']} • summary from {item.get('summary_provider', row['source'])} • {stamp}\n{item['url']}"
            )
        errors = [
            symbol
            for symbol in symbols
            if self._health(symbol).get("error") or self._health(symbol).get("primary_error")
        ]
        if errors:
            lines.append("News source gaps/fallbacks: " + ", ".join(sorted(errors)))
        return lines
