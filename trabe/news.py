"""Compact sentiment reports backed by bounded news extracts and provenance."""

import html
import json
import logging
import math
import re
import time
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from xml.etree import ElementTree

from trabe.ledger import Ledger
from trabe.signals import SignalStore, WorkStopped

WINDOW = 7 * 86400
PER_SYMBOL = 20
GLOBAL_LIMIT = 1000
logger = logging.getLogger(__name__)
TRACKING = {"fbclid", "gclid", "guccounter", "guce_referrer", "guce_referrer_sig"}


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
            key.lower() in {"token", "apikey", "api_key", "api_token", "access_token", "secret"}
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


class News:
    def __init__(self, store, http):
        from trabe.news_store import NewsStore

        self.store, self.http = store, http
        self.repo = SignalStore(store)
        self.news_store = NewsStore(store)

    def _receipt(self, now):
        received = getattr(self.http, "received_at", now)
        return (
            max(now, received)
            if type(received) in (int, float) and math.isfinite(received)
            else now
        )

    @staticmethod
    def _timestamp(value, now):
        if type(value) not in (int, float) or not math.isfinite(value):
            return None
        return float(value) if now - WINDOW <= value <= now + 2 else None

    def _normalize(self, raw, symbol, source, now):
        headline, snippet = text(raw.get("headline"), 512), text(raw.get("summary"), 4096)
        url = canonical_url(raw.get("url"))
        published = self._timestamp(raw.get("datetime"), now)
        related = raw.get("related")
        if (
            isinstance(related, str)
            and related
            and symbol not in set(re.split(r"[,;\s]+", related.upper()))
        ):
            return None
        if not headline or not url or published is None:
            return None
        identity = raw.get("id")
        return {
            "headline": headline,
            "snippet": snippet,
            "url": url,
            "provider": source,
            "item_id": str(identity)[:150] if type(identity) in (str, int) else url,
            "publisher": text(raw.get("source"), 160) or source,
            "published_at": published,
            "input_quality": "snippet" if snippet else "headline_only",
            "annotations": raw.get("annotations", []),
            "correction_of": str(raw.get("correction_of", ""))[:150],
        }

    def _rss(self, symbol, now):
        url = "https://feeds.finance.yahoo.com/rss/2.0/headline?" + urlencode(
            {"s": symbol, "region": "US", "lang": "en-US"}
        )
        body = self.http.xml(url, provider="yahoo")
        now = self._receipt(now)
        if isinstance(body, str):
            body = body.encode()
        if (
            not isinstance(body, bytes)
            or len(body) > 2_000_000
            or b"<!DOCTYPE" in body.upper()
            or b"<!ENTITY" in body.upper()
        ):
            raise ValueError("Invalid Yahoo feed")
        root = ElementTree.fromstring(body)
        if root.tag.rsplit("}", 1)[-1] != "rss":
            raise ValueError("Unexpected Yahoo feed")
        items = []
        for item in root.findall(".//item")[:100]:
            try:
                published = parsedate_to_datetime(item.findtext("pubDate", ""))
                if published.tzinfo is None:
                    continue
                stamp = published.timestamp()
            except (TypeError, ValueError, OverflowError):
                continue
            normalized = self._normalize(
                {
                    "id": item.findtext("guid", ""),
                    "headline": item.findtext("title", ""),
                    "summary": item.findtext("description", ""),
                    "url": item.findtext("link", ""),
                    "datetime": stamp,
                    "source": "Yahoo Finance RSS",
                },
                symbol,
                "yahoo-rss",
                now,
            )
            if normalized:
                items.append(normalized)
        return items

    def company_context(self, symbol, now):
        from trabe.news_quality import context

        try:
            profile = json.loads(self.store.state("finnhub_profile_" + symbol, "{}"))
            cached = json.loads(self.store.state("news_context_" + symbol, "{}"))
        except (ValueError, TypeError):
            profile, cached = {}, {}
        name = text(profile.get("name"), 160)
        if cached and 0 <= now - cached.get("checked_at", 0) < 7 * 86400:
            return context(symbol, name or cached.get("name", ""), cached.get("aliases", []))
        company = context(symbol, name)
        if not company["aliases"] and hasattr(self.http, "finnhub"):
            try:
                raw = self.http.finnhub("stock/profile2", symbol=symbol)
                if isinstance(raw, dict) and raw.get("ticker") == symbol:
                    name = text(raw.get("name"), 160)
                    company = context(symbol, name)
            except WorkStopped:
                raise
            except Exception:
                pass
        self.store.set_state("news_context_" + symbol, json.dumps(company | {"checked_at": now}))
        return company

    def _merge(self, items, symbol, source, now, *, save_view=True):
        from trabe.news_analysis import RulesAnalyzer
        from trabe.news_quality import digest
        from trabe.news_store import encoded

        company = self.company_context(symbol, now)
        analyzer = RulesAnalyzer()
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            for item in sorted(items, key=lambda i: i["published_at"], reverse=True)[:100]:
                revision, observation = self.news_store.observe(item, symbol, now, db)
                input_hash = analyzer.input_hash(item, company)
                cached = db.execute(
                    "SELECT payload FROM news_analyses WHERE ticker=? AND origin='rules' AND method=? AND input_hash=? ORDER BY id DESC LIMIT 1",
                    (symbol, analyzer.method, input_hash),
                ).fetchone()
                analysis = json.loads(cached[0]) if cached else analyzer.analyze(item, company)
                self.news_store.save_analysis(revision, symbol, analysis, now, db=db)
                for annotation in item.get("annotations", [])[:2]:
                    if annotation.get("ticker") == symbol:
                        result = annotation | {
                            "provider": source,
                            "provider_article_id": item["item_id"][:150],
                            "input_hash": digest(encoded(annotation) + str(observation)),
                        }
                        self.news_store.save_analysis(
                            revision,
                            symbol,
                            result,
                            now,
                            origin="vendor",
                            observation_id=observation,
                            db=db,
                        )
        if save_view:
            self._save_view(symbol, now)

    def _compute(self, symbol, now, db=None):
        from trabe.news_quality import aggregate, group_events

        rows = self.news_store.records(symbol, now, db)
        groups = group_events(rows, symbol, now)
        view = aggregate(groups, symbol, now, PER_SYMBOL)
        if db is None:
            capped = float(self.store.state("news_retention_" + symbol, "0"))
        else:
            row = db.execute(
                "SELECT value FROM state WHERE key=?", ("news_retention_" + symbol,)
            ).fetchone()
            capped = float(row[0]) if row else 0
        view["truncated"] |= bool(capped and 0 <= now - capped < 10800)
        return view, groups

    def _article_items(self, items, symbol, now, cache):
        from trabe.articles import article_url, sentences
        from trabe.news_quality import digest, mentions

        company = self.company_context(symbol, now)
        enriched = []
        for item in sorted(items, key=lambda item: item["published_at"], reverse=True):
            if not article_url(item["url"]) or not mentions(
                item["headline"] + " " + item["snippet"], company
            ):
                continue
            try:
                document = cache.get(item["url"], self._receipt(now))
            except WorkStopped:
                raise
            except Exception:
                continue
            if not document or not mentions(document["headline"], company):
                continue
            snippet = sentences(document, company, maximum=1500)
            observed = self._receipt(now)
            if not snippet or self._timestamp(document["published_at"], observed) is None:
                continue
            enriched.append(
                {
                    "headline": document["headline"],
                    "snippet": snippet,
                    "url": item["url"],
                    "provider": "yahoo-article",
                    "item_id": digest(item["url"]),
                    "publisher": "Yahoo Finance article",
                    "published_at": document["published_at"],
                    "input_quality": "article",
                    "annotations": [],
                    "correction_of": "",
                }
            )
        return enriched

    def _save_view(self, symbol, now):
        view, groups = self._compute(symbol, now)
        self.news_store.save_view(symbol, view, groups, now)
        return view

    def _records(self, symbols=None, now=None, limit=None):
        # Compatibility reader for diagnostics; the revision store is authoritative.
        now = __import__("time").time() if now is None else now
        if symbols is None:
            with self.store.connection() as db:
                symbols = [r[0] for r in db.execute("SELECT DISTINCT ticker FROM news_targets")]
        records = {}
        for symbol in symbols:
            for row in self.news_store.records(symbol, now):
                key = row["article_id"]
                if key not in records:
                    records[key] = {
                        "key": key,
                        "symbol": symbol,
                        "source": row["provider"],
                        "event_time": row["published_at"],
                        "available_at": row["observed_at"],
                        "observed_at": row["observed_at"],
                        "payload": {
                            "headline": row["headline"],
                            "snippet": row["snippet"],
                            "summary": row["snippet"],
                            "url": row["url"],
                            "canonical_url": row["canonical_url"],
                            "publisher": row["publisher"],
                            "symbols": [],
                            "published_at": row["published_at"],
                            "first_seen_at": row["observed_at"],
                            "sentiment": row["analysis"]["label"],
                            "score": row["analysis"]["bias"],
                            "summary_method": "supplied-input",
                            "input_quality": row["input_quality"],
                        },
                    }
                if symbol not in records[key]["payload"]["symbols"]:
                    records[key]["payload"]["symbols"].append(symbol)
        return sorted(records.values(), key=lambda r: r["event_time"], reverse=True)[
            : limit or GLOBAL_LIMIT
        ]

    def _trim(self, now, symbols=None):
        self.news_store.prune(
            now, global_limit=GLOBAL_LIMIT, per_symbol=PER_SYMBOL, tickers=symbols
        )

    def _health(self, symbol):
        try:
            return json.loads(self.store.state("news_health_" + symbol, "{}"))
        except (ValueError, TypeError):
            return {}

    def _source_health(self, symbol, provider, state, now, **extra):
        self.store.set_state(
            "news_source_" + provider + "_" + symbol,
            json.dumps({"state": state, "checked_at": now, **extra}),
        )

    def refresh(self, symbols, *, now, force=False):
        from trabe.articles import ArticleCache
        from trabe.news_sources import INTERVALS, NewsSources

        selected = self.store.settings()["news_sources"].split(",")
        adapters = NewsSources(self.http, self._normalize, text)
        result = {"refreshed": 0, "failed": 0, "items": 0}
        for symbol in dict.fromkeys(symbols):
            articles = ArticleCache(self.store, self.http)
            health = self._health(symbol)
            changed, successful = False, False
            for provider in [
                p
                for p in ("finnhub", "yahoo-rss", "massive", "marketaux")
                if ("yahoo" if p == "yahoo-rss" else p) in selected
            ]:
                if not force and not self.repo.due(
                    "news", provider, symbol, INTERVALS[provider], now
                ):
                    continue
                if provider in ("massive", "marketaux") and not getattr(
                    getattr(self.http, "config", None), provider + "_key", ""
                ):
                    self._source_health(symbol, provider, "key missing", now)
                    continue
                self.repo.attempt("news", provider, symbol, now)
                changed = True
                started = time.monotonic()
                phase = "fetch"
                try:
                    truncated = False
                    if provider == "finnhub":
                        start = datetime.fromtimestamp(now - WINDOW, UTC).date().isoformat()
                        end = datetime.fromtimestamp(now, UTC).date().isoformat()
                        raw = self.http.finnhub(
                            "company-news", symbol=symbol, **{"from": start, "to": end}
                        )
                        now = self._receipt(now)
                        if not isinstance(raw, list):
                            raise ValueError("Invalid Finnhub news response")
                        candidates = sorted(
                            (
                                r
                                for r in raw[:2000]
                                if isinstance(r, dict) and type(r.get("datetime")) in (int, float)
                            ),
                            key=lambda r: r["datetime"],
                            reverse=True,
                        )
                        items = [
                            i
                            for row in candidates[:100]
                            if (i := self._normalize(row, symbol, provider, now))
                        ]
                        truncated = len(raw) > 100
                    elif provider == "yahoo-rss":
                        items = self._rss(symbol, now)
                        now = self._receipt(now)
                    else:
                        items, truncated = adapters.fetch(provider, symbol, now)
                        now = self._receipt(now)
                    phase = "processing"
                    fetched = time.monotonic()
                    feed_received = now
                    self._merge(items, symbol, provider, now, save_view=False)
                    enriched = (
                        self._article_items(items, symbol, now, articles)
                        if "yahoo" in selected
                        else []
                    )
                    if enriched:
                        self._merge(
                            enriched, symbol, "yahoo-article", self._receipt(now), save_view=False
                        )
                    now = self._receipt(now)
                    logger.info(
                        "News %s/%s: %d items; fetch %.2fs, processing %.2fs",
                        symbol,
                        provider,
                        len(items),
                        fetched - started,
                        time.monotonic() - fetched,
                    )
                    self.repo.ok("news", provider, symbol, feed_received)
                    self._source_health(
                        symbol,
                        provider,
                        "empty response" if not items else "truncated" if truncated else "ok",
                        feed_received,
                        items=len(items),
                        truncated=truncated,
                    )
                    successful = True
                    result["items"] += len(items)
                    health.update(source=provider, checked_at=now, last_success=now, error="")
                    if provider == "finnhub":
                        health["primary_error"] = ""
                except WorkStopped:
                    raise
                except Exception as error:
                    now = self._receipt(now)
                    code = getattr(error, "code", 0)
                    state = (
                        "budget deferred"
                        if str(error) == "News request budget deferred."
                        else "cooldown"
                        if code == 429
                        else "failed"
                    )
                    message = f"{provider.title()} unavailable (HTTP {code or 'network/data'})"
                    self.repo.fail(
                        "news",
                        provider,
                        symbol,
                        message,
                        now,
                        retry=max(3600, getattr(error, "retry_after", 0)),
                    )
                    self._source_health(symbol, provider, state, now, error=message)
                    logger.warning(
                        "News %s/%s failed during %s (%s, HTTP %s)",
                        symbol,
                        provider,
                        phase,
                        type(error).__name__,
                        code or "none",
                    )
                    health.update(checked_at=now)
                    if provider == "finnhub":
                        health["primary_error"] = message
                    if not successful:
                        health["error"] = "Refresh unavailable; cached news retained"
            if changed:
                self._trim(now, [symbol])
                self._save_view(symbol, now)
                self.store.set_state("news_health_" + symbol, json.dumps(health))
                result["refreshed" if successful else "failed"] += 1
        return result

    @staticmethod
    def _age(published, now):
        minutes = max(0, int(now - published)) // 60
        return (
            f"{minutes}m"
            if minutes < 60
            else f"{minutes // 60}h"
            if minutes < 1440
            else f"{minutes // 1440}d"
        )

    def _card(self, symbol, view, now, *, detail=False, extracts=False):
        bias = view["bias"]
        signed = (
            "0.0"
            if bias is not None and abs(bias) < 0.05
            else f"{bias:+.1f}"
            if bias is not None
            else ""
        )
        icon = {"Bullish": "🟢", "Bearish": "🔴", "Neutral": "⚪"}.get(view["label"], "⚪")
        lines = [f"{icon} {symbol} • {view['label']}" + (f" ({signed})" if signed else "")]
        if not view["event_count"]:
            lines.append("No recent cached news.")
        else:
            flags = " • mixed signals" if view["mixed"] else ""
            flags += " • sector/macro context only" if view["contextual_only"] else ""
            lines.append(
                f"{view['event_count']} distinct {'story' if view['event_count'] == 1 else 'stories'}{flags}"
            )
            for item in view["evidence"][: 3 if detail else 1]:
                lines.append(
                    f"{item['headline'][:160]} • {self._age(item['published_at'], now)} ago"
                )
                if detail or extracts:
                    quality = (
                        "Article extract"
                        if item["input_quality"] == "article"
                        else "Feed extract"
                        if item["snippet"]
                        else "Headline only"
                    )
                    lines.append(
                        quality + (": " + item["snippet"][:240] if item["snippet"] else ".")
                    )
        if view["truncated"]:
            lines.append("Limited collected coverage.")
        if self.store.settings()["news_second_view"] == "on" and (
            {"massive", "marketaux"}.intersection(self.store.settings()["news_sources"].split(","))
            or view["second_view"]["matched"]
        ):
            second = view["second_view"]
            state = second["state"]
            lines.append(
                f"Second view: {state}"
                + (f" • {second['matched']} matched stories" if second["matched"] else "")
            )
            if second["different_context"]:
                lines.append("Second view uses different text scope.")
        return "\n".join(lines)

    def _warnings(self, symbols, now):
        lines = []
        for symbol in dict.fromkeys(symbols):
            health = self._health(symbol)
            notes = []
            if health.get("error"):
                notes.append("refresh unavailable; cached news retained")
            elif health.get("primary_error") and health.get("source") == "yahoo-rss":
                notes.append("using fallback feed")
            elif health.get("primary_error") and "finnhub" in self.store.settings()[
                "news_sources"
            ].split(","):
                notes.append("primary feed unavailable; additional source used")
            if health.get("last_success") and now - health["last_success"] > 10800:
                notes.append("stale cache")
            if notes:
                lines.append(f"⚠️ {symbol}: " + " • ".join(notes))
        return lines

    @staticmethod
    def _article_context(symbol, groups, now):
        candidates = [
            g
            for g in groups
            if g["representative"]["input_quality"] == "article"
            and g["representative"]["analysis"]["relevance"] == "direct"
        ]
        if not candidates:
            return None
        group = max(candidates, key=lambda g: g["published_at"])
        row = group["representative"]
        return f"{symbol} • Additional article context • {News._age(group['published_at'], now)} ago\n{row['headline'][:160]}\nArticle extract: {row['snippet'][:240]}"

    def report(self, symbols, *, now):
        symbols = list(dict.fromkeys(symbols))
        held = Ledger(self.store).held()
        symbols.sort(key=lambda ticker: ticker not in held)
        if not symbols:
            return "News: no held or watched symbols."
        lines = ["📰 News sentiment"]
        for symbol in symbols[:10]:
            view, groups = self._compute(symbol, now)
            lines.extend(("", self._card(symbol, view, now, detail=len(symbols) == 1)))
            if len(symbols) == 1 and not any(
                i["input_quality"] == "article" for i in view["evidence"]
            ):
                extra = self._article_context(symbol, groups, now)
                if extra:
                    lines.extend(("", extra))
        lines.extend(self._warnings(symbols[:10], now))
        if len(symbols) > 10:
            lines.append(f"Showing 10/{len(symbols)} companies; request /news TICKER.")
        elif len(symbols) > 1:
            lines.append("\nMore: /news TICKER")
        lines.append("\nSentiment is heuristic, not a price forecast.")
        return "\n".join(lines)

    def alerts(self, symbols, *, now):
        return []

    def briefing(self, symbols, *, now, limit=3):
        held = Ledger(self.store).held()
        selected = sorted(dict.fromkeys(symbols), key=lambda s: (s not in held, s))
        lines, extra, has_article = [], None, False
        shown = max(1, min(limit, 5))
        for index, symbol in enumerate(selected[:10]):
            view, groups = self._compute(symbol, now)
            if index < shown:
                lines.append(self._card(symbol, view, now, extracts=True))
                has_article |= bool(
                    view["evidence"] and view["evidence"][0]["input_quality"] == "article"
                )
            if not extra:
                extra = self._article_context(symbol, groups, now)
            if index >= shown - 1 and (extra or has_article):
                break
        if extra and not has_article:
            lines.append(extra)
        return [*lines, *self._warnings(selected, now)] if lines else ["No recent cached news."]

    def sources_status(self, symbols):
        selected = self.store.settings()["news_sources"].split(",")
        config = getattr(self.http, "config", None)
        lines = ["News sources:"]
        for source in ("finnhub", "yahoo", "massive", "marketaux"):
            enabled = source in selected
            key = (
                "public RSS"
                if source == "yahoo"
                else "key configured"
                if getattr(config, source + "_key", "")
                else "key missing"
            )
            budget = self.store.settings().get(source + "_daily_budget")
            lines.append(
                f"{source}: {'enabled' if enabled else 'off'} • {key}"
                + (
                    f" • {self.store.provider_usage(source)}/{budget} attempts today"
                    if budget
                    else ""
                )
            )
        for row in self.repo.status("news"):
            if row["symbol"] in symbols:
                raw = self.store.state("news_source_" + row["source"] + "_" + row["symbol"], "{}")
                health = json.loads(raw)
                lines.append(
                    f"{row['symbol']} / {row['source']}: {health.get('state', 'not attempted')} • {health.get('items', 0)} returned"
                    + (" • " + row["error"] if row["error"] else "")
                )
        lines.append("Configure: /set news_sources finnhub,yahoo,massive")
        return "\n".join(lines)
