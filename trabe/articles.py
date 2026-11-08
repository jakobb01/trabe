"""Shared, bounded public article documents; no scripts, models or paywall bypass."""

import hashlib
import json
import re
from datetime import datetime
from html.parser import HTMLParser
from urllib.parse import urlsplit

from trabe.signals import WorkStopped

PROVIDER = "yahoo-article"
FRESH_SECONDS = 6 * 3600
MAX_DOCUMENT_BYTES = 128 * 1024
MAX_DOCUMENTS = 32


class Article(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.metadata, self.parts, self.script = [], [], []
        self.skipped = 0
        self.article_depth = 0
        self.jsonld = False

    def handle_starttag(self, tag, attrs):
        if tag == "article":
            self.article_depth += 1
        if tag in {"script", "style", "svg"}:
            self.skipped += 1
        if tag == "script":
            self.jsonld = dict(attrs).get("type") == "application/ld+json"
            self.script = []

    def handle_endtag(self, tag):
        if tag == "script" and self.jsonld:
            try:
                raw = json.loads("".join(self.script))
                items = raw if isinstance(raw, list) else raw.get("@graph", [raw])
                self.metadata.extend(x for x in items[:32] if isinstance(x, dict))
            except (ValueError, TypeError, AttributeError):
                pass
            self.jsonld = False
        if tag in {"script", "style", "svg"}:
            self.skipped = max(0, self.skipped - 1)
        if tag == "article":
            self.article_depth = max(0, self.article_depth - 1)

    def handle_data(self, data):
        if self.jsonld:
            self.script.append(data)
        if self.article_depth and not self.skipped:
            self.parts.append(data)


def parse_document(body, now):
    if not isinstance(body, bytes) or len(body) > 2_000_000:
        return None
    parser = Article()
    parser.feed(body.decode("utf-8", "replace"))
    articles = [m for m in parser.metadata if m.get("@type") in ("NewsArticle", "Article")]
    if len(articles) != 1:
        return None
    metadata = articles[0]
    try:
        published = datetime.fromisoformat(metadata["datePublished"].replace("Z", "+00:00"))
        if not published.tzinfo or not 0 < published.timestamp() <= now:
            return None
    except (KeyError, TypeError, ValueError, AttributeError):
        return None
    if not isinstance(metadata.get("headline"), str) or not metadata["headline"].strip():
        return None
    text = " ".join(" ".join(parser.parts).split())[:100000]
    if not text:
        return None
    return {
        "headline": metadata["headline"][:512],
        "published_iso": metadata["datePublished"],
        "published_at": published.timestamp(),
        "text": text,
        "text_hash": hashlib.sha256(text.encode()).hexdigest(),
        "received_at": now,
    }


def article_url(url):
    from trabe.news import canonical_url

    url = canonical_url(url)
    if not url:
        return None
    parts = urlsplit(url)
    if (
        parts.scheme != "https"
        or parts.hostname != "finance.yahoo.com"
        or parts.port not in (None, 443)
    ):
        return None
    return (
        url if re.fullmatch(r"/(?:news|markets/[^/]+/articles)/[^/]+\.html", parts.path) else None
    )


def sentences(document, company=None, *, maximum=600):
    from trabe.news_quality import mentions

    text = document["text"]
    header = re.search(
        r"\b(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)[a-z]*,\s+[A-Z][a-z]+\s+\d{1,2},\s+\d{4}\b.{0,120}?\b\d+\s+min read\b",
        text[:2000],
        re.I,
    )
    if header:
        text = text[header.end() :].strip()
    candidates = re.split(r"(?<=[.!?])\s+", text)
    selected = []
    for sentence in candidates:
        if len(sentence) < 30 or re.search(
            r"click here|read more|sign up|subscribe now", sentence, re.I
        ):
            continue
        if company and not mentions(sentence, company):
            continue
        selected.append(sentence)
        if len(selected) == 2:
            break
    return " ".join(selected)[:maximum]


class ArticleCache:
    def __init__(self, store, http, *, max_fetches=2):
        self.store, self.http = store, http
        self.max_fetches, self.fetches = max_fetches, 0

    def get(self, url, now):
        url = article_url(url)
        if url is None:
            return None
        key = hashlib.sha256(url.encode()).hexdigest()
        with self.store.connection() as db:
            cached = db.execute(
                "SELECT payload,fetched_at FROM signal_http_cache WHERE provider=? AND cache_key=?",
                (PROVIDER, key),
            ).fetchone()
        if cached:
            payload = json.loads(cached[0])
            if cached[1] > now:
                return None
            lifetime = (
                3600 if payload.get("failed") or payload.get("document") is None else FRESH_SECONDS
            )
            if now - cached[1] < lifetime:
                if payload.get("failed"):
                    raise ValueError("Public article temporarily unavailable.")
                return payload.get("document")
        if self.fetches >= self.max_fetches:
            return None
        self.fetches += 1
        try:
            body = self.http.xml(url, provider="yahoo")
            received = max(now, getattr(self.http, "received_at", now))
            document = parse_document(body, received)
            if document:
                document["url"] = url
            payload = {"document": document}
        except WorkStopped:
            raise
        except Exception:
            received = max(now, getattr(self.http, "received_at", now))
            payload = {"failed": True, "document": None}
        raw = json.dumps(payload, ensure_ascii=False, allow_nan=False)
        if len(raw.encode()) <= MAX_DOCUMENT_BYTES:
            with self.store.connection() as db:
                db.execute("BEGIN IMMEDIATE")
                db.execute(
                    "INSERT OR REPLACE INTO signal_http_cache VALUES (?,?,?,?)",
                    (PROVIDER, key, raw, received),
                )
                db.execute(
                    "DELETE FROM signal_http_cache WHERE provider=? AND (fetched_at<? OR cache_key NOT IN (SELECT cache_key FROM signal_http_cache WHERE provider=? ORDER BY fetched_at DESC,cache_key LIMIT ?))",
                    (PROVIDER, received - 7 * 86400, PROVIDER, MAX_DOCUMENTS),
                )
        if payload.get("failed"):
            raise ValueError("Public article temporarily unavailable.")
        return payload["document"]
