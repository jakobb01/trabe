"""Opt-in material event delivery. Atomic enqueue, bounded identities, no exactly-once claim."""

from datetime import datetime
from zoneinfo import ZoneInfo

from trabe.news_digest import card
from trabe.news_event_store import PAYLOAD_LIMIT, EventStore, generation, state
from trabe.news_events import digest, present_event, safe_projection


def quiet(settings, now):
    if settings["news_quiet_enabled"] != "on":
        return False
    local = datetime.fromtimestamp(now, ZoneInfo(settings["timezone"])).strftime("%H:%M")
    start, end = settings["news_quiet_start"], settings["news_quiet_end"]
    if start == end:
        return True
    return start <= local < end if start < end else local >= start or local < end


def enabled(db, now):
    settings = dict(db.execute("SELECT key,value FROM settings"))
    return (
        settings["news_enabled"] == settings["news_alerts_enabled"] == "on"
        and float(state(db, "pause_until", "0")) <= now
        and not state(db, "storage_paused")
    )


class NewsPush:
    def __init__(self, store, owner_id):
        self.store, self.owner_id = store, owner_id
        self.repo = EventStore(store)

    def run_once(self, symbols, now):
        from trabe.ledger import Ledger
        from trabe.outbox import Outbox

        with self.store.connection() as db:
            if not enabled(db, now):
                return 0
            expected = generation(db)
        events = self.repo.events(symbols, now, context=True, audit=True)
        count = 0
        for event in events:
            with self.store.connection() as db:
                db.execute("BEGIN IMMEDIATE")
                if not enabled(db, now) or generation(db) != expected:
                    return count
                if self.repo.payload_bytes(db) > PAYLOAD_LIMIT - 16384:
                    self.repo._gap(db, now)
                    continue
                settings = self.store.settings(db)
                # Automatic pause expiry is a resume too; do not replay quiet backlog.
                pause_until = float(state(db, "pause_until", "0"))
                watermark = max(float(state(db, "news_event_watermark", "0")), pause_until)
                universe = Ledger._held(db) | {
                    r[0] for r in db.execute("SELECT ticker FROM watchlist")
                }
                if not set(event["issuers"]).intersection(universe):
                    continue
                prior = db.execute(
                    "SELECT COUNT(*) FROM event_deliveries WHERE event_id=? AND enqueued IS NOT NULL",
                    (event["id"],),
                ).fetchone()[0]
                if (
                    prior
                    and db.execute(
                        "SELECT 1 FROM event_deliveries WHERE event_id=? AND revision=? AND enqueued IS NOT NULL",
                        (event["id"], event["revision"]),
                    ).fetchone()
                ):
                    continue
                correction = bool(event.get("correction") and prior)
                eligible = event["push_eligible"] or correction
                if (
                    not eligible
                    or (not correction and event["ticker"] not in universe.intersection(symbols))
                    or event["seeded"]
                    or event["ambiguous"]
                    or event["known_at"] <= watermark
                    or prior >= 2
                    or (not correction and now - event["published"] > 86400)
                ):
                    continue
                # An initial delivery and one substantive follow-up across all generations.
                slot = "followup" if prior else "initial"
                key = digest(f"{self.owner_id}:{event['id']}:{slot}")
                existing = db.execute(
                    "SELECT status,generation,revision FROM event_deliveries WHERE semantic_key=?",
                    (key,),
                ).fetchone()
                expires = (event["known_at"] if correction else event["published"]) + 86400
                if expires <= now:
                    continue
                if existing and (existing[0] != "pending" or existing[1] != expected):
                    continue
                if not existing:
                    if (
                        db.execute(
                            "SELECT (SELECT COUNT(*) FROM event_deliveries)+(SELECT COUNT(*) FROM event_delivery_aliases)"
                        ).fetchone()[0]
                        >= 2000
                    ):
                        self.repo._gap(db, now)
                        continue
                    db.execute(
                        "INSERT INTO event_deliveries VALUES (?,?,?,?,?,'pending','quiet_or_cap',?,?,NULL,NULL,NULL)",
                        (
                            key,
                            event["id"],
                            event["revision"],
                            self.owner_id,
                            expected,
                            now,
                            expires,
                        ),
                    )
                elif existing[2] != event["revision"]:
                    # Coalesce unsent facts; this does not create another notification.
                    db.execute(
                        "UPDATE event_deliveries SET revision=?,expires=? WHERE semantic_key=?",
                        (event["revision"], expires, key),
                    )
                if quiet(settings, now):
                    continue
                day = datetime.fromtimestamp(now, ZoneInfo(settings["timezone"])).date().isoformat()
                cap = int(settings["news_alert_daily_cap"])
                daily = db.execute(
                    "SELECT COUNT(*) FROM event_deliveries WHERE enqueued IS NOT NULL AND local_day=?",
                    (day,),
                ).fetchone()[0]
                rolling = db.execute(
                    "SELECT COUNT(*) FROM event_deliveries WHERE enqueued>?", (now - 86400,)
                ).fetchone()[0]
                if daily >= cap or rolling >= cap:
                    db.execute(
                        "UPDATE event_deliveries SET status='suppressed',reason='cap_to_briefing' WHERE semantic_key=?",
                        (key,),
                    )
                    continue
                class_alias = digest(
                    "event-class:" + event["ticker"] + ":" + event["type"] + ":" + event["action"]
                )
                prior_class = db.execute(
                    "SELECT event_id FROM event_delivery_aliases WHERE alias=? AND created>=?",
                    (class_alias, now - 14 * 86400),
                ).fetchone()
                if prior_class and prior_class[0] != event["id"]:
                    db.execute(
                        "UPDATE event_deliveries SET status='suppressed',reason='possible_event_overlap' WHERE semantic_key=?",
                        (key,),
                    )
                    continue
                fact_hash = db.execute(
                    "SELECT facts_hash FROM event_revisions WHERE id=?", (event["revision"],)
                ).fetchone()[0]
                aliases = [
                    digest("url:" + event["sources"][0]["url"]),
                    digest("fact:" + fact_hash),
                    class_alias,
                ]
                if (
                    db.execute(
                        "SELECT (SELECT COUNT(*) FROM event_delivery_aliases)+(SELECT COUNT(*) FROM event_deliveries)"
                    ).fetchone()[0]
                    + len(aliases)
                    > 2000
                ):
                    self.repo._gap(db, now)
                    continue
                db.executemany(
                    "INSERT OR IGNORE INTO event_delivery_aliases VALUES (?,?,?)",
                    [(alias, event["id"], now) for alias in aliases],
                )
                db.execute(
                    "UPDATE event_delivery_aliases SET event_id=?,created=? WHERE alias=? AND created<?",
                    (event["id"], now, class_alias, now - 14 * 86400),
                )
                # Display selection is downstream of frozen routing and dedupe.
                display_symbols = event["issuers"] if correction else [event["ticker"]]
                view = (
                    present_event(event, self.repo.credited_source(db, event), display_symbols)
                    or event
                )
                outbox_id = Outbox(self.store).send(
                    self.owner_id,
                    "📰 Company news alert\n" + card(view, compact=True, now=now),
                    kind="news_event",
                    db=db,
                    now=now,
                )
                db.execute(
                    "UPDATE event_deliveries SET status='queued',reason='',enqueued=?,local_day=?,outbox_id=? WHERE semantic_key=?",
                    (now, day, outbox_id, key),
                )
                count += 1
        self.expire(now)
        return count

    def expire(self, now, db=None):
        if db is None:
            with self.store.connection() as connection:
                return self.expire(now, connection)
        db.execute(
            "UPDATE event_deliveries SET status='expired',reason='freshness_expired' WHERE status IN ('pending','queued') AND expires<=?",
            (now,),
        )
        db.execute(
            "DELETE FROM outbox WHERE kind='news_event' AND id IN (SELECT outbox_id FROM event_deliveries WHERE status='expired')"
        )

    @staticmethod
    def flush_policy(store, db, item_id, now):
        row = db.execute(
            "SELECT generation,expires,status FROM event_deliveries WHERE outbox_id=?", (item_id,)
        ).fetchone()
        reason = ""
        if not row or row[2] != "queued":
            reason = "missing_or_cancelled"
        elif row[1] <= now:
            reason = "freshness_expired"
        elif not enabled(db, now) or row[0] != generation(db):
            reason = "disabled_generation"
        if not reason:
            import json

            from trabe.ledger import Ledger

            event = db.execute(
                "SELECT r.payload FROM event_revisions r JOIN event_deliveries d ON d.revision=r.id WHERE d.outbox_id=?",
                (item_id,),
            ).fetchone()
            universe = Ledger._held(db) | {r[0] for r in db.execute("SELECT ticker FROM watchlist")}
            if not event or not set(json.loads(event[0])["issuers"]).intersection(universe):
                reason = "outside_universe"
            else:
                projected = safe_projection(json.loads(event[0]))
                if not (
                    (projected["push_eligible"] and projected["ticker"] in universe)
                    or projected.get("correction")
                ):
                    reason = "attribution_unresolved"
        if reason:
            db.execute(
                "UPDATE event_deliveries SET status='suppressed',reason=? WHERE outbox_id=?",
                (reason, item_id),
            )
            db.execute("DELETE FROM outbox WHERE id=?", (item_id,))
            return "drop"
        return "hold" if quiet(store.settings(db), now) else "send"
