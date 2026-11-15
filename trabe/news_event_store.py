"""Bounded presentation audit, isolated from NewsStore and strategy inputs."""

import json
import time
import uuid

from trabe.news_events import (
    ALIASES,
    analyze,
    digest,
    encoded,
    facts_hash,
    safe_projection,
    sentences,
    words,
)

TTL = 14 * 86400
PAYLOAD_LIMIT = 16 * 1024 * 1024
EVENT_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS event_evidence(
        id TEXT PRIMARY KEY,provider TEXT NOT NULL,item_id TEXT NOT NULL,url TEXT NOT NULL,
        content_hash TEXT NOT NULL,published REAL NOT NULL,received REAL NOT NULL,payload TEXT NOT NULL)""",
    "CREATE INDEX IF NOT EXISTS evidence_identity ON event_evidence(provider,item_id)",
    "CREATE INDEX IF NOT EXISTS evidence_url ON event_evidence(url)",
    """CREATE TABLE IF NOT EXISTS event_targets(
        evidence TEXT NOT NULL REFERENCES event_evidence(id) ON DELETE CASCADE,
        ticker TEXT NOT NULL,PRIMARY KEY(evidence,ticker))""",
    """CREATE TABLE IF NOT EXISTS news_events(
        id TEXT PRIMARY KEY,signature TEXT NOT NULL,first_seen REAL NOT NULL,last_seen REAL NOT NULL,
        published REAL NOT NULL,materiality TEXT NOT NULL,ambiguous INTEGER NOT NULL DEFAULT 0)""",
    "CREATE INDEX IF NOT EXISTS event_signature ON news_events(signature)",
    "CREATE INDEX IF NOT EXISTS event_targets_ticker ON event_targets(ticker,evidence)",
    """CREATE TABLE IF NOT EXISTS event_revisions(
        id TEXT PRIMARY KEY,event_id TEXT NOT NULL REFERENCES news_events(id) ON DELETE CASCADE,
        facts_hash TEXT NOT NULL,known_at REAL NOT NULL,payload TEXT NOT NULL,
        UNIQUE(event_id,facts_hash))""",
    """CREATE TABLE IF NOT EXISTS event_bindings(
        event_id TEXT NOT NULL REFERENCES news_events(id) ON DELETE CASCADE,
        revision TEXT NOT NULL REFERENCES event_revisions(id) ON DELETE CASCADE,
        evidence TEXT NOT NULL REFERENCES event_evidence(id) ON DELETE CASCADE,
        PRIMARY KEY(revision,evidence))""",
    """CREATE TABLE IF NOT EXISTS event_collection(
        source TEXT NOT NULL,ticker TEXT NOT NULL,payload TEXT NOT NULL,
        PRIMARY KEY(source,ticker))""",
    "CREATE INDEX IF NOT EXISTS event_binding_evidence ON event_bindings(evidence)",
    "CREATE INDEX IF NOT EXISTS event_binding_event ON event_bindings(event_id)",
    "CREATE INDEX IF NOT EXISTS event_revision_facts ON event_revisions(facts_hash)",
    "CREATE INDEX IF NOT EXISTS event_revision_latest ON event_revisions(event_id,known_at DESC)",
    # No FK: delivery identities outlive evidence and event pruning.
    """CREATE TABLE IF NOT EXISTS event_deliveries(
        semantic_key TEXT PRIMARY KEY,event_id TEXT NOT NULL,revision TEXT NOT NULL,
        audience INTEGER NOT NULL,generation INTEGER NOT NULL,status TEXT NOT NULL,
        reason TEXT NOT NULL,created REAL NOT NULL,expires REAL NOT NULL,
        enqueued REAL,local_day TEXT,outbox_id INTEGER)""",
    "CREATE INDEX IF NOT EXISTS event_delivery_time ON event_deliveries(enqueued)",
    """CREATE TABLE IF NOT EXISTS event_delivery_aliases(
        alias TEXT PRIMARY KEY,event_id TEXT NOT NULL,created REAL NOT NULL)""",
    """CREATE TRIGGER IF NOT EXISTS event_revision_immutable BEFORE UPDATE ON event_revisions
        BEGIN SELECT RAISE(ABORT,'immutable event revision'); END""",
)


def state(db, key, default=""):
    row = db.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


def generation(db):
    return int(state(db, "news_event_generation", "0"))


class EventStore:
    def __init__(self, store):
        self.store = store

    def coverage(self, source, ticker, now, *, db=None, **values):
        if db is None:
            with self.store.connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                return self.coverage(source, ticker, now, db=connection, **values)
        old = db.execute(
            "SELECT payload FROM event_collection WHERE source=? AND ticker=?", (source, ticker)
        ).fetchone()
        payload = (json.loads(old[0]) if old else {}) | values | {"attempted_at": now}
        raw = encoded(payload)
        if len(raw.encode()) > 4096:
            raise ValueError("Collection state too large.")
        db.execute("SAVEPOINT event_coverage")
        db.execute("INSERT OR REPLACE INTO event_collection VALUES (?,?,?)", (source, ticker, raw))
        if self.payload_bytes(db) > PAYLOAD_LIMIT - 1024:
            db.execute("ROLLBACK TO event_coverage")
            db.execute("RELEASE event_coverage")
            self._gap(db, now)
            return False
        db.execute("RELEASE event_coverage")
        return True

    @staticmethod
    def payload_bytes(db):
        # Include metadata, bindings and escaped JSON payloads: a conservative
        # serialized budget distinct from SQLite's pages, indexes and WAL.
        total = 0
        for table in (
            "event_evidence",
            "event_revisions",
            "event_collection",
            "news_events",
            "event_targets",
            "event_bindings",
            "event_deliveries",
            "event_delivery_aliases",
        ):
            columns = [r[1] for r in db.execute(f"PRAGMA table_info({table})")]
            fields = ",".join(f"'{c}',{c}" for c in columns)
            total += db.execute(
                f"SELECT COALESCE(SUM(length(CAST(json_object({fields}) AS BLOB))),0) FROM {table}"
            ).fetchone()[0]
        for table, condition in (
            ("outbox", "kind='news_event'"),
            ("state", "key LIKE 'event_%' OR key LIKE 'news_event_%' OR key='massive_extra_pages'"),
        ):
            columns = [r[1] for r in db.execute(f"PRAGMA table_info({table})")]
            fields = ",".join(f"'{c}',{c}" for c in columns)
            total += db.execute(
                f"SELECT COALESCE(SUM(length(CAST(json_object({fields}) AS BLOB))),0) FROM {table} WHERE {condition}"
            ).fetchone()[0]
        return total

    @staticmethod
    def pinned(db):
        return {
            r[0]
            for r in db.execute(
                "SELECT event_id FROM event_deliveries WHERE status IN ('pending','queued')"
            )
        }

    def _gap(self, db, now):
        db.execute("INSERT OR REPLACE INTO state VALUES ('event_retention_partial',?)", (str(now),))

    def prune(self, now, db=None):
        if db is None:
            with self.store.connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                return self.prune(now, connection)
        db.execute(
            "DELETE FROM event_deliveries WHERE created<? AND status NOT IN ('pending','queued')",
            (now - 30 * 86400,),
        )
        db.execute("DELETE FROM event_delivery_aliases WHERE created<?", (now - 30 * 86400,))
        pins = self.pinned(db)
        for event_id, published in db.execute(
            "SELECT e.id,MAX(r.known_at) FROM news_events e JOIN event_revisions r ON r.event_id=e.id GROUP BY e.id ORDER BY e.published,e.id"
        ).fetchall():
            if published < now - TTL and event_id not in pins:
                db.execute("DELETE FROM news_events WHERE id=?", (event_id,))
        db.execute(
            "DELETE FROM event_evidence WHERE received<? AND id NOT IN (SELECT evidence FROM event_bindings)",
            (now - TTL,),
        )
        db.execute(
            "DELETE FROM event_collection WHERE CAST(json_extract(payload,'$.attempted_at') AS REAL)<?",
            (now - TTL,),
        )
        self._orphans(db)
        for (event_id,) in db.execute("SELECT id FROM news_events").fetchall():
            if event_id not in pins:
                db.execute(
                    "DELETE FROM event_revisions WHERE event_id=? AND id NOT IN (SELECT id FROM event_revisions WHERE event_id=? ORDER BY known_at DESC,rowid DESC LIMIT 4)",
                    (event_id, event_id),
                )
        self._orphans(db)
        while self._over(db):
            over_tickers = [
                r[0]
                for r in db.execute(
                    "SELECT ticker FROM event_targets GROUP BY ticker HAVING COUNT(*)>100"
                )
            ]
            where = ""
            if over_tickers:
                marks = ",".join("?" for _ in over_tickers)
                where = f"WHERE id IN (SELECT b.event_id FROM event_bindings b JOIN event_targets t ON t.evidence=b.evidence WHERE t.ticker IN ({marks}))"
            rows = db.execute(
                "SELECT id FROM news_events "
                + where
                + " ORDER BY CASE materiality WHEN 'low' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END,published,id",
                over_tickers,
            ).fetchall()
            victim = next((r[0] for r in rows if r[0] not in pins), None)
            if victim is None:
                break
            db.execute("DELETE FROM news_events WHERE id=?", (victim,))
            self._orphans(db)
            self._gap(db, now)
        return not self._over(db)

    @staticmethod
    def _orphans(db):
        db.execute(
            "DELETE FROM event_evidence WHERE id NOT IN (SELECT evidence FROM event_bindings)"
        )

    def _over(self, db):
        return (
            db.execute("SELECT COUNT(*) FROM event_evidence").fetchone()[0] > 1000
            or db.execute("SELECT COUNT(*) FROM news_events").fetchone()[0] > 250
            or bool(
                db.execute(
                    "SELECT ticker FROM event_targets GROUP BY ticker HAVING COUNT(*)>100 LIMIT 1"
                ).fetchone()
            )
            or db.execute("SELECT COUNT(*) FROM event_deliveries").fetchone()[0] > 2000
            or bool(
                db.execute(
                    "SELECT event_id FROM event_revisions GROUP BY event_id HAVING COUNT(*)>4 LIMIT 1"
                ).fetchone()
            )
            or db.execute(
                "SELECT (SELECT COUNT(*) FROM event_delivery_aliases)+(SELECT COUNT(*) FROM event_deliveries)"
            ).fetchone()[0]
            > 2000
            or self.payload_bytes(db) > PAYLOAD_LIMIT - 1024
        )

    def ingest(
        self,
        items,
        ticker,
        now,
        *,
        source="mirror",
        expected_generation=None,
        guard=None,
        seeded=False,
        collection=None,
    ):
        prepared = []
        for item in items[:100]:
            if not 0 <= now - item["published_at"] <= TTL:
                continue
            targets = [s for s in dict.fromkeys([ticker, *ALIASES]) if analyze(item, s)["issuer"]]
            analyses = [analyze(item, s) for s in targets]
            # Subject first; multiple explicit issuers remain visible and digest-only.
            from trabe.news_events import contains

            def position(analysis):
                aliases = ALIASES.get(analysis["ticker"], (analysis["ticker"],))
                positions = [
                    item["headline"].lower().find(alias.lower())
                    for alias in aliases
                    if contains(item["headline"], alias)
                ]
                return (not analysis["direct"], min(positions, default=9999), analysis["ticker"])

            analyses.sort(key=position)
            primary = analyses[0] if analyses else analyze(item, ticker)
            primary = primary | {"issuers": targets, "seeded": seeded}
            primary["push_eligible"] &= len([a for a in analyses if a["direct"]]) == 1
            payload = {
                k: item.get(k, "")
                for k in ("headline", "snippet", "publisher", "role", "input_quality")
            }
            payload["sentences"] = sentences(item)
            # Keep only complete bounded source sentences; label omitted body honestly.
            while len(encoded(payload).encode()) > 8192 and payload["sentences"]:
                payload["snippet"] = ""
                payload["sentences"].pop()
                payload["partial"] = True
            if len(encoded(primary).encode()) > 4096 or len(encoded(payload).encode()) > 8192:
                continue
            prepared.append((item, targets or [ticker], primary, payload))
        # Short independent write windows keep polling, outbox and price workers
        # responsive on the Pi. The page cursor commits only with the final chunk;
        # partial pages safely replay evidence identities after interruption.
        chunks = [prepared[i : i + 10] for i in range(0, len(prepared), 10)] or [[]]
        for index, chunk in enumerate(chunks):
            with self.store.connection() as db:
                db.execute("BEGIN IMMEDIATE")
                if expected_generation is not None and generation(db) != expected_generation:
                    return False
                if guard and not guard(db):
                    return False
                db.execute("SAVEPOINT event_batch")
                for item, targets, analysis, payload in chunk:
                    self._observe(db, item, targets, analysis, payload, now)
                bounded = self.prune(now, db)
                if bounded and collection and index == len(chunks) - 1:
                    bounded = self.coverage(source, ticker, now, db=db, **collection)
                if not bounded:
                    db.execute("ROLLBACK TO event_batch")
                    self._gap(db, now)
                    self.coverage(
                        source,
                        ticker,
                        now,
                        db=db,
                        state="cap-deferred",
                        gap="Pending evidence retained; new work deferred",
                    )
                    db.execute("RELEASE event_batch")
                    return False
                db.execute("RELEASE event_batch")
            if index < len(chunks) - 1:
                time.sleep(0)
        return True

    def _observe(self, db, item, targets, analysis, payload, now):
        content = digest(encoded(payload))
        evidence = digest(encoded([item["provider"], item["item_id"], content]))
        fact_hash = facts_hash(analysis)
        signature = digest(
            encoded([analysis["issuers"], words(item["headline"]), analysis["event_date"]])
        )
        old = db.execute(
            "SELECT b.event_id FROM event_bindings b JOIN event_evidence e ON e.id=b.evidence WHERE e.url=? OR (e.provider=? AND e.item_id IN (?,?)) ORDER BY e.received DESC LIMIT 1",
            (item["url"], item["provider"], item["item_id"], item.get("correction_of", "")),
        ).fetchone()
        exact = db.execute(
            "SELECT r.event_id FROM event_revisions r JOIN news_events e ON e.id=r.event_id WHERE r.facts_hash=? LIMIT 1",
            (fact_hash,),
        ).fetchone()
        alias = db.execute(
            "SELECT event_id FROM event_delivery_aliases WHERE alias IN (?,?) ORDER BY created LIMIT 1",
            (digest("url:" + item["url"]), digest("fact:" + fact_hash)),
        ).fetchone()
        event_id = (old or exact or alias or [uuid.uuid4().hex[:20]])[0]
        ambiguous = bool(
            not old
            and not exact
            and db.execute("SELECT 1 FROM news_events WHERE signature=?", (signature,)).fetchone()
        )
        if not (old or exact or alias) and analysis["push_eligible"]:
            # Similar reports stay separate, but must not create duplicate-prone
            # pushes merely because providers rewrote the same material event.
            ambiguous |= bool(
                db.execute(
                    "SELECT 1 FROM news_events e JOIN event_revisions r ON r.event_id=e.id WHERE e.last_seen>=? AND json_extract(r.payload,'$.issuer')=? AND json_extract(r.payload,'$.type')=? AND json_extract(r.payload,'$.action')=? LIMIT 1",
                    (now - TTL, analysis["issuer"], analysis["type"], analysis["action"]),
                ).fetchone()
            )
        db.execute(
            "INSERT INTO news_events VALUES (?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET last_seen=excluded.last_seen",
            (
                event_id,
                signature,
                now,
                now,
                item["published_at"],
                analysis["materiality"],
                int(ambiguous),
            ),
        )
        revision = digest(event_id + fact_hash)[:24]
        prior = db.execute(
            "SELECT id FROM event_revisions WHERE event_id=? ORDER BY known_at DESC,rowid DESC LIMIT 1",
            (event_id,),
        ).fetchone()
        analysis["correction"] = bool(
            item.get("correction_of")
            or item["headline"].lower().startswith(("correction:", "retraction:"))
        )
        if old and prior and analysis["correction"]:
            previous = db.execute(
                "SELECT payload FROM event_revisions WHERE id=?", (prior[0],)
            ).fetchone()
            # Routing follows the explicit source-item correction binding, even
            # when the withdrawal itself omits the issuer's name. Actor stays unknown.
            analysis["issuers"] = sorted(
                set(analysis["issuers"]) | set(json.loads(previous[0])["issuers"])
            )
        if len(encoded(analysis).encode()) > 4096:
            raise ValueError("Event revision exceeds its payload limit.")
        db.execute(
            "INSERT OR IGNORE INTO event_revisions VALUES (?,?,?,?,?)",
            (revision, event_id, fact_hash, now, encoded(analysis)),
        )
        if prior and prior[0] != revision:
            db.execute(
                "UPDATE news_events SET materiality=? WHERE id=?",
                (analysis["materiality"], event_id),
            )
        db.execute(
            "INSERT OR IGNORE INTO event_evidence VALUES (?,?,?,?,?,?,?,?)",
            (
                evidence,
                item["provider"],
                item["item_id"],
                item["url"],
                content,
                item["published_at"],
                now,
                encoded(payload),
            ),
        )
        targets = sorted(set(targets) | set(analysis["issuers"]))
        db.executemany(
            "INSERT OR IGNORE INTO event_targets VALUES (?,?)", [(evidence, s) for s in targets]
        )
        db.execute(
            "INSERT OR IGNORE INTO event_bindings VALUES (?,?,?)", (event_id, revision, evidence)
        )

    def events(self, symbols, now, *, context=False, audit=False):
        with self.store.connection() as db:
            window = TTL if audit else 7 * 86400 if context else 72 * 3600
            rows = db.execute(
                """SELECT e.id,e.first_seen,e.published,e.ambiguous,r.id,r.known_at,r.payload
                FROM news_events e JOIN event_revisions r ON r.event_id=e.id
                WHERE r.id=(SELECT id FROM event_revisions WHERE event_id=e.id AND known_at<=? ORDER BY known_at DESC,rowid DESC LIMIT 1)
                AND ((e.published BETWEEN ? AND ?) OR (json_extract(r.payload,'$.correction')=1 AND r.known_at BETWEEN ? AND ?))""",
                (now, now - window, now, now - window, now),
            ).fetchall()
            result = []
            for event_id, first, published, ambiguous, revision, known, raw in rows:
                analysis = safe_projection(json.loads(raw))
                if not set(analysis["issuers"]).intersection(symbols):
                    continue
                sources = []
                for provider, url, received, publisher in db.execute(
                    "SELECT e.provider,e.url,e.received,json_extract(e.payload,'$.publisher') FROM event_evidence e JOIN event_bindings b ON b.evidence=e.id WHERE b.revision=? AND e.received<=? ORDER BY CASE json_extract(e.payload,'$.role') WHEN 'issuer' THEN 0 ELSE 1 END,e.received,e.id",
                    (revision, now),
                ):
                    # Digests consume frozen facts and small source metadata, not
                    # every retained full extract/sentence object in the audit.
                    sources.append(
                        {
                            "publisher": publisher or provider,
                            "provider": provider,
                            "url": url,
                            "received": received,
                        }
                    )
                if sources:
                    result.append(
                        analysis
                        | {
                            "id": event_id,
                            "revision": revision,
                            "known_at": known,
                            "first_seen": first,
                            "published": published,
                            "ambiguous": bool(ambiguous),
                            "sources": sources,
                        }
                    )
            return sorted(
                result,
                key=lambda e: (
                    -{"high": 3, "medium": 2, "low": 1}[e["materiality"]],
                    -e["known_at"],
                    not e["direct"],
                    e["quality"] == "headline_only",
                    e["id"],
                ),
            )

    def coverage_lines(self, symbols, now):
        with self.store.connection() as db:
            rows = db.execute(
                "SELECT source,ticker,payload FROM event_collection ORDER BY ticker,source"
            ).fetchall()
            capped = state(db, "event_retention_partial")
        lines = []
        for source, ticker, raw in rows:
            if ticker not in symbols and ticker != "*":
                continue
            item = json.loads(raw)
            status = (
                "stale"
                if now - item.get("attempted_at", 0) > 10800
                else item.get("state", "partial")
            )
            lines.append(
                f"{ticker}/{source}: {status}" + (" • " + item["gap"] if item.get("gap") else "")
            )
        error = self.store.state("event_processing_error")
        if error:
            lines.append("Presentation processing failed; cached cards retained.")
        if capped:
            lines.append("Retained audit partial: storage caps shortened coverage.")
        return lines or ["Company sources not refreshed; coverage unverified."]

    def seed(self, db, now):
        # No analysis of historical packets, no fabricated observation timestamp.
        # Existing rows are mirrored lazily after the next successful refresh.
        db.execute(
            "INSERT OR IGNORE INTO state VALUES ('event_seed_policy','next-refresh; legacy history partial')"
        )


def invalidate(db, now, reason):
    """Settings generations also invalidate workers that were already in flight."""
    db.execute(
        "INSERT INTO state VALUES ('news_event_generation','1') ON CONFLICT(key) DO UPDATE SET value=CAST(value AS INTEGER)+1"
    )
    db.execute("INSERT OR REPLACE INTO state VALUES ('news_event_watermark',?)", (str(now),))
    db.execute(
        "UPDATE event_deliveries SET status='suppressed',reason=? WHERE status IN ('pending','queued')",
        (reason,),
    )
    db.execute("DELETE FROM outbox WHERE kind='news_event'")
