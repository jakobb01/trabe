"""Bounded news provenance; immutable content and assessment occurrences."""

import json
import math
import time

from trabe.news_quality import digest

NEWS_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS news_articles(
        id TEXT PRIMARY KEY,url TEXT NOT NULL,first_seen REAL NOT NULL,
        representative_revision INTEGER)""",
    """CREATE TABLE IF NOT EXISTS news_revisions(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        article_id TEXT NOT NULL REFERENCES news_articles(id) ON DELETE CASCADE,
        provider TEXT NOT NULL,item_id TEXT NOT NULL,sequence INTEGER NOT NULL,
        input_hash TEXT NOT NULL,headline TEXT NOT NULL,snippet TEXT NOT NULL,
        publisher TEXT NOT NULL,published_at REAL NOT NULL,observed_at REAL NOT NULL,
        input_quality TEXT NOT NULL,correction_of TEXT NOT NULL DEFAULT '',url TEXT NOT NULL DEFAULT '')""",
    "CREATE INDEX IF NOT EXISTS news_stream ON news_revisions(provider,item_id,id)",
    "CREATE INDEX IF NOT EXISTS news_article_history ON news_revisions(article_id,observed_at)",
    """CREATE TABLE IF NOT EXISTS news_provider_observations(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        revision_id INTEGER NOT NULL REFERENCES news_revisions(id) ON DELETE CASCADE,
        provider TEXT NOT NULL,item_id TEXT NOT NULL,annotations TEXT NOT NULL,
        annotation_hash TEXT NOT NULL,observed_at REAL NOT NULL,last_seen REAL NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS news_targets(
        revision_id INTEGER REFERENCES news_revisions(id) ON DELETE CASCADE,
        ticker TEXT NOT NULL,evidence TEXT NOT NULL,observed_at REAL NOT NULL,
        PRIMARY KEY(revision_id,ticker))""",
    """CREATE TABLE IF NOT EXISTS news_analyses(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        revision_id INTEGER NOT NULL REFERENCES news_revisions(id) ON DELETE CASCADE,
        ticker TEXT NOT NULL,origin TEXT NOT NULL,method TEXT NOT NULL,
        input_hash TEXT NOT NULL,payload TEXT NOT NULL,bound_at REAL NOT NULL,
        observation_id INTEGER REFERENCES news_provider_observations(id) ON DELETE CASCADE,
        reused_analysis_id INTEGER)""",
    "CREATE INDEX IF NOT EXISTS news_analysis_target ON news_analyses(revision_id,ticker,origin,id)",
    "CREATE INDEX IF NOT EXISTS news_analysis_input ON news_analyses(ticker,method,input_hash,id)",
    """CREATE TABLE IF NOT EXISTS news_event_members(
        id INTEGER PRIMARY KEY AUTOINCREMENT,event_id TEXT NOT NULL,ticker TEXT NOT NULL,
        revision_id INTEGER REFERENCES news_revisions(id) ON DELETE CASCADE,
        grouping_version TEXT NOT NULL,assigned_at REAL NOT NULL,
        UNIQUE(event_id,ticker,revision_id,grouping_version))""",
)


def encoded(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def object_row(cursor, row):
    return dict(zip((column[0] for column in cursor.description), row))


class NewsStore:
    def __init__(self, store):
        self.store = store

    def observe(self, item, ticker, now, db=None):
        if db is None:
            with self.store.connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                return self.observe(item, ticker, now, connection)
        if not math.isfinite(now):
            raise ValueError("Invalid news observation time.")
        provider = item["provider"]
        item_id = item.get("item_id") or item["url"]
        previous = db.execute(
            "SELECT id,article_id,sequence,input_hash FROM news_revisions WHERE provider=? AND item_id=? ORDER BY id DESC LIMIT 1",
            (provider, item_id),
        ).fetchone()
        article_id = previous[1] if previous else digest(item["url"])
        db.execute(
            "INSERT OR IGNORE INTO news_articles(id,url,first_seen) VALUES (?,?,?)",
            (article_id, item["url"], now),
        )
        content_hash = digest(
            encoded(
                {
                    k: item.get(k, "")
                    for k in (
                        "headline",
                        "snippet",
                        "input_quality",
                        "published_at",
                        "correction_of",
                        "url",
                    )
                }
            )
        )
        if previous and previous[3] == content_hash:
            revision_id = previous[0]
        else:
            revision_id = db.execute(
                "INSERT INTO news_revisions(article_id,provider,item_id,sequence,input_hash,headline,snippet,publisher,published_at,observed_at,input_quality,correction_of,url) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    article_id,
                    provider,
                    item_id,
                    previous[2] + 1 if previous else 1,
                    content_hash,
                    item["headline"][:512],
                    item.get("snippet", "")[:4096],
                    item.get("publisher", provider)[:160],
                    item["published_at"],
                    now,
                    item.get("input_quality", "headline_only"),
                    item.get("correction_of", "")[:150],
                    item["url"],
                ),
            ).lastrowid
        annotations = list(item.get("annotations", []))[:20]
        while len(encoded(annotations).encode()) > 4096:
            annotations.pop()
        raw = encoded(annotations)
        annotation_hash = digest(raw)
        old = db.execute(
            "SELECT id,annotation_hash FROM news_provider_observations WHERE revision_id=? ORDER BY id DESC LIMIT 1",
            (revision_id,),
        ).fetchone()
        if old and old[1] == annotation_hash:
            observation_id = old[0]
            db.execute(
                "UPDATE news_provider_observations SET last_seen=? WHERE id=?",
                (now, observation_id),
            )
        else:
            observation_id = db.execute(
                "INSERT INTO news_provider_observations(revision_id,provider,item_id,annotations,annotation_hash,observed_at,last_seen) VALUES (?,?,?,?,?,?,?)",
                (revision_id, provider, item_id, raw, annotation_hash, now, now),
            ).lastrowid
        self.associate(
            revision_id, ticker, "provider query/tag; relevance checked separately", now, db
        )
        db.execute(
            "UPDATE news_articles SET representative_revision=? WHERE id=? AND representative_revision IS NULL",
            (revision_id, article_id),
        )
        return revision_id, observation_id

    def associate(self, revision_id, ticker, evidence, now, db=None):
        if db is None:
            with self.store.connection() as connection:
                return self.associate(revision_id, ticker, evidence, now, connection)
        db.execute(
            "INSERT OR IGNORE INTO news_targets VALUES (?,?,?,?)",
            (revision_id, ticker, evidence[:240], now),
        )

    def save_analysis(
        self, revision_id, ticker, result, now, *, origin="rules", observation_id=None, db=None
    ):
        if db is None:
            with self.store.connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                return self.save_analysis(
                    revision_id,
                    ticker,
                    result,
                    now,
                    origin=origin,
                    observation_id=observation_id,
                    db=connection,
                )
        bias = result.get("bias")
        if bias is not None and (
            type(bias) not in (int, float) or not math.isfinite(bias) or not -1 <= bias <= 1
        ):
            raise ValueError("Invalid news bias.")
        if origin not in {"rules", "model", "vendor"}:
            raise ValueError("Invalid news analysis origin.")
        if origin != "vendor":
            if result.get("relevance") not in {
                "direct",
                "contextual",
                "unrelated",
                "unknown",
            } or result.get("importance") not in (0.2, 0.6, 1.0):
                raise ValueError("Invalid news analysis classification.")
            from trabe.news_quality import label

            if result.get("label") != label(bias) or type(result.get("mixed")) is not bool:
                raise ValueError("Invalid news label or mixed-signal flag.")
            if type(result.get("importance")) not in (int, float):
                raise ValueError("Invalid news importance.")
            source = db.execute(
                "SELECT headline,snippet FROM news_revisions WHERE id=?", (revision_id,)
            ).fetchone()
            supplied = "\n".join(source or ())
            if any(
                not isinstance(span, dict) or not span.get("text") or span["text"] not in supplied
                for span in result.get("supporting_spans", [])
            ):
                raise ValueError("News reason is not grounded in supplied text.")
        elif observation_id is None:
            raise ValueError("Vendor scores require a source observation.")
        if origin == "vendor" and result.get("direction") not in {
            "positive",
            "neutral",
            "negative",
        }:
            raise ValueError("Invalid vendor sentiment direction.")
        if origin == "vendor" and (
            result.get("ticker") != ticker
            or not db.execute(
                "SELECT 1 FROM news_provider_observations WHERE id=? AND revision_id=?",
                (observation_id, revision_id),
            ).fetchone()
        ):
            raise ValueError("Vendor score does not match its company/source input.")
        raw = encoded(result)
        if len(raw.encode()) > 8192:
            raise ValueError("News analysis exceeds its bound.")
        method, input_hash = result["method"], result["input_hash"]
        prior = db.execute(
            "SELECT id,payload FROM news_analyses WHERE revision_id=? AND ticker=? AND origin=? AND method=? AND observation_id IS ? ORDER BY id DESC LIMIT 1",
            (revision_id, ticker, origin, method, observation_id),
        ).fetchone()
        if prior and prior[1] == raw:
            return prior[0]
        reused = db.execute(
            "SELECT id FROM news_analyses WHERE ticker=? AND method=? AND input_hash=? ORDER BY id DESC LIMIT 1",
            (ticker, method, input_hash),
        ).fetchone()
        return db.execute(
            "INSERT INTO news_analyses(revision_id,ticker,origin,method,input_hash,payload,bound_at,observation_id,reused_analysis_id) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                revision_id,
                ticker,
                origin,
                method,
                input_hash,
                raw,
                now,
                observation_id,
                reused[0] if reused else None,
            ),
        ).lastrowid

    def records(self, ticker, now, db=None):
        if db is None:
            with self.store.connection() as connection:
                return self.records(ticker, now, connection)
        cursor = db.execute(
            """SELECT r.*,t.observed_at AS associated_at,a.url AS canonical_url,
            n.payload AS analysis,n.bound_at,
            (SELECT MIN(prior.published_at) FROM news_revisions prior WHERE prior.provider=r.provider AND prior.item_id=r.item_id
                AND prior.headline=r.headline AND prior.snippet=r.snippet AND prior.correction_of=r.correction_of AND prior.observed_at<=?
                AND prior.id>COALESCE((SELECT MAX(changed.id) FROM news_revisions changed
                    WHERE changed.provider=r.provider AND changed.item_id=r.item_id AND changed.id<r.id
                    AND (changed.headline!=r.headline OR changed.snippet!=r.snippet OR changed.correction_of!=r.correction_of)),0)) AS first_publication
            FROM news_revisions r
            JOIN news_articles a ON a.id=r.article_id
            JOIN news_targets t ON t.revision_id=r.id AND t.ticker=?
            JOIN news_analyses n ON n.revision_id=r.id AND n.ticker=t.ticker AND n.origin='rules'
            WHERE r.observed_at<=? AND t.observed_at<=? AND n.bound_at<=?
            AND n.id=(SELECT MAX(id) FROM news_analyses WHERE revision_id=r.id AND ticker=t.ticker AND origin='rules' AND bound_at<=?)
            AND NOT EXISTS(SELECT 1 FROM news_revisions newer WHERE newer.provider=r.provider AND newer.item_id=r.item_id AND newer.id>r.id AND newer.observed_at<=?)
            AND NOT EXISTS(SELECT 1 FROM news_revisions correction JOIN news_targets ct ON ct.revision_id=correction.id
                JOIN news_revisions replaced ON replaced.provider=correction.provider AND replaced.item_id=correction.correction_of
                WHERE replaced.article_id=r.article_id AND ct.ticker=t.ticker AND correction.observed_at<=? AND ct.observed_at<=?
                AND NOT EXISTS(SELECT 1 FROM news_revisions newer WHERE newer.provider=correction.provider AND newer.item_id=correction.item_id AND newer.id>correction.id AND newer.observed_at<=?))
            ORDER BY r.published_at DESC LIMIT 1000""",
            (now, ticker, now, now, now, now, now, now, now, now),
        )
        rows = [object_row(cursor, row) for row in cursor]
        for row in rows:
            row["analysis"] = json.loads(row["analysis"])
            row["ticker"] = ticker
            vendor = db.execute(
                """SELECT n.payload,n.bound_at,o.observed_at FROM news_analyses n
                JOIN news_provider_observations o ON o.id=n.observation_id
                WHERE n.revision_id=? AND n.ticker=? AND n.origin='vendor' AND n.bound_at<=? AND o.observed_at<=?
                AND o.id=(SELECT MAX(id) FROM news_provider_observations WHERE revision_id=? AND observed_at<=?)
                ORDER BY n.id DESC LIMIT 2""",
                (row["id"], ticker, now, now, row["id"], now),
            ).fetchall()
            row["vendor_scores"] = [
                json.loads(v[0]) | {"known_at": max(v[1], v[2], row["associated_at"])}
                for v in vendor
            ]
        return rows

    def save_view(self, ticker, view, groups, now):
        from trabe.news_quality import representative_key

        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            for group in groups:
                representatives = {}
                for row in group["members"]:
                    prior = representatives.get(row["article_id"])
                    if prior is None or representative_key(row) > representative_key(prior):
                        representatives[row["article_id"]] = row
                    db.execute(
                        "INSERT OR IGNORE INTO news_event_members(event_id,ticker,revision_id,grouping_version,assigned_at) VALUES (?,?,?,?,?)",
                        (group["id"], ticker, row["id"], view["method"], now),
                    )
                for article_id, representative in representatives.items():
                    db.execute(
                        "UPDATE news_articles SET representative_revision=? WHERE id=?",
                        (representative["id"], article_id),
                    )
            db.execute(
                "INSERT OR REPLACE INTO state VALUES (?,?)", ("news_view_" + ticker, encoded(view))
            )

    def view(self, ticker, db, now):
        row = db.execute("SELECT value FROM state WHERE key=?", ("news_view_" + ticker,)).fetchone()
        if not row:
            return None
        view = json.loads(row[0])
        if view.get("as_of", now + 1) > now:
            return None
        return view

    def prune(self, now, db=None, *, global_limit=1000, per_symbol=20, tickers=None):
        if db is None:
            with self.store.connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                return self.prune(
                    now,
                    connection,
                    global_limit=global_limit,
                    per_symbol=per_symbol,
                    tickers=tickers,
                )
        from datetime import datetime, timedelta

        from trabe.calendar import NY, retention_cutoff

        cutoff = datetime.combine(
            retention_cutoff(datetime.fromtimestamp(now, NY).date()) + timedelta(days=1),
            datetime.min.time(),
            NY,
        ).timestamp()
        db.execute(
            "DELETE FROM news_revisions WHERE observed_at<? OR published_at<?", (cutoff, cutoff)
        )
        for table, cap in (
            ("news_revisions", 5000),
            ("news_provider_observations", 10000),
            ("news_analyses", 10000),
            ("news_event_members", 10000),
        ):
            db.execute(
                f"DELETE FROM {table} WHERE id NOT IN (SELECT id FROM {table} ORDER BY id DESC LIMIT ?)",
                (cap,),
            )
        db.execute(
            "DELETE FROM news_targets WHERE rowid NOT IN (SELECT rowid FROM news_targets ORDER BY observed_at DESC LIMIT 10000)"
        )
        from trabe.news_quality import event_weight, group_events

        # Choose distinct event representatives before spending slots on copies.
        # Keep provider variants/annotations together wherever the article cap allows.
        selected = (
            [(ticker,) for ticker in tickers]
            if tickers is not None
            else db.execute("SELECT DISTINCT ticker FROM news_targets").fetchall()
        )
        for (ticker,) in selected:
            rows = self.records(ticker, now, db)
            groups = sorted(
                group_events(rows, ticker, now),
                key=lambda g: (event_weight(g, now), g["published_at"]),
                reverse=True,
            )
            ids = list(
                dict.fromkeys(g["representative"]["article_id"] for g in groups[:per_symbol])
            )
            for row in rows:
                if len(ids) >= per_symbol:
                    break
                if row["article_id"] not in ids:
                    ids.append(row["article_id"])
            if ids:
                db.execute(
                    "DELETE FROM news_targets WHERE ticker=? AND revision_id IN (SELECT id FROM news_revisions WHERE article_id NOT IN ("
                    + ",".join("?" for _ in ids)
                    + "))",
                    [ticker, *ids],
                )
                if len({r["article_id"] for r in rows}) > per_symbol:
                    db.execute(
                        "INSERT OR REPLACE INTO state VALUES (?,?)",
                        ("news_retention_" + ticker, str(now)),
                    )
        db.execute(
            "DELETE FROM news_articles WHERE id NOT IN (SELECT r.article_id FROM news_revisions r JOIN news_targets t ON t.revision_id=r.id)"
        )
        db.execute(
            "DELETE FROM news_articles WHERE id NOT IN (SELECT article_id FROM news_revisions GROUP BY article_id ORDER BY MAX(published_at) DESC LIMIT ?)",
            (global_limit,),
        )


def migrate_legacy(db, now=None):
    """Import extracts once at migration, without inventing historical availability."""
    from trabe.news import canonical_url
    from trabe.news_analysis import RulesAnalyzer
    from trabe.news_quality import context

    now = time.time() if now is None else now
    rows = db.execute(
        "SELECT payload FROM signals WHERE kind='news' ORDER BY event_time DESC LIMIT 1000"
    ).fetchall()
    repo = NewsStore(None)
    for (raw,) in rows:
        old = json.loads(raw)
        url = canonical_url(old.get("canonical_url"))
        if not url or not old.get("headline"):
            continue
        item = {
            "url": url,
            "headline": old["headline"][:512],
            "snippet": old.get("summary", "")[:4096],
            "provider": "legacy",
            "item_id": old["canonical_url"],
            "publisher": old.get("publisher", "legacy"),
            "published_at": old["published_at"],
            "input_quality": "legacy_extract",
        }
        for ticker in old.get("symbols", [])[:100]:
            revision, _ = repo.observe(item, ticker, now, db)
            repo.save_analysis(
                revision, ticker, RulesAnalyzer().analyze(item, context(ticker)), now, db=db
            )
