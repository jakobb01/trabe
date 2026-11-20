"""Private weather preferences, provider cache and durable local-slot delivery."""

import json
import random
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from hashlib import sha256
from zoneinfo import ZoneInfo

from trabe import weather_met as met

WEATHER_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS weather_settings(
        id INTEGER PRIMARY KEY CHECK(id=1), timezone TEXT NOT NULL,
        coordinates TEXT NOT NULL DEFAULT '', label TEXT NOT NULL DEFAULT '',
        times TEXT NOT NULL DEFAULT '07:00', days INTEGER NOT NULL DEFAULT 127,
        enabled INTEGER NOT NULL DEFAULT 0, generation INTEGER NOT NULL DEFAULT 0,
        changed_at REAL NOT NULL DEFAULT 0, contact TEXT NOT NULL DEFAULT '',
        accepted_by INTEGER NOT NULL DEFAULT 0, terms TEXT NOT NULL DEFAULT '',
        prompt TEXT NOT NULL DEFAULT '', prompt_until REAL NOT NULL DEFAULT 0,
        preview_until REAL NOT NULL DEFAULT 0, throttle_until REAL NOT NULL DEFAULT 0,
        delivery_status TEXT NOT NULL DEFAULT '')""",
    """CREATE TABLE IF NOT EXISTS weather_cache(
        coordinates TEXT PRIMARY KEY, attempted_at REAL NOT NULL, next_at REAL NOT NULL,
        expires REAL, modified TEXT NOT NULL DEFAULT '', payload TEXT,
        fetched_at REAL NOT NULL DEFAULT 0, checked_at REAL NOT NULL DEFAULT 0,
        status TEXT NOT NULL DEFAULT '', deprecated INTEGER NOT NULL DEFAULT 0)""",
    """CREATE TABLE IF NOT EXISTS weather_slots(
        day TEXT NOT NULL, hhmm TEXT NOT NULL, queued INTEGER NOT NULL,
        PRIMARY KEY(day,hhmm))""",
    """CREATE TABLE IF NOT EXISTS weather_deliveries(
        outbox_id INTEGER PRIMARY KEY REFERENCES outbox(id) ON DELETE CASCADE,
        generation INTEGER NOT NULL, coordinates TEXT NOT NULL, expires REAL NOT NULL,
        slot REAL)""",
)
CACHE_LIMIT = 16
SLOT_WINDOW = 30 * 60
PREVIEW_WINDOW = 5 * 60
PROMPT_WINDOW = 5 * 60
DAY_NAMES = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
WEATHER_UPDATES = {
    "coordinates",
    "label",
    "times",
    "days",
    "timezone",
    "enabled",
    "contact",
    "accepted_by",
    "terms",
}
KINDS = ("weather", "weather_preview")


def preferences(db):
    row = db.execute("SELECT * FROM weather_settings WHERE id=1")
    return dict(zip((column[0] for column in row.description), row.fetchone()))


def update_preferences(db, now, **values):
    if not values.keys() <= WEATHER_UPDATES:
        raise ValueError("Unknown weather preference.")
    before = preferences(db)
    changes = {key: value for key, value in values.items() if value != before[key]}
    if changes:
        assignments = ",".join(key + "=?" for key in changes)
        db.execute(
            f"""UPDATE weather_settings SET {assignments},generation=generation+1,
                changed_at=?,preview_until=0,delivery_status='' WHERE id=1""",
            (*changes.values(), now),
        )
        db.execute("DELETE FROM outbox WHERE kind IN ('weather','weather_preview')")
    db.execute("UPDATE weather_settings SET prompt='',prompt_until=0 WHERE id=1")


def access_problem(settings, owner):
    if not settings["coordinates"]:
        return "Set a location."
    if settings["accepted_by"] != owner or settings["terms"] != met.TERMS_REVISION:
        return "Review and accept the MET terms in Provider setup."
    if not settings["contact"]:
        return "Add a real service contact in Provider setup."
    return ""


def guard_problem(db, now):
    state = dict(
        db.execute("SELECT key,value FROM state WHERE key IN ('pause_until','storage_paused')")
    )
    if float(state.get("pause_until", 0)) > now:
        return "Global automation is paused."
    if state.get("storage_paused"):
        return "Weather is paused by the storage guard. See /storage."
    return ""


@dataclass(frozen=True, order=True)
class Slot:
    stamp: float
    day: str
    hhmm: str


def slots_on(settings, day: date, zone):
    if not settings["days"] & (1 << day.weekday()):
        return []
    slots = []
    for hhmm in settings["times"].split():
        local = datetime.fromisoformat(f"{day}T{hhmm}")
        # Use the first occurrence of a repeated wall-clock time.
        stamp = local.replace(tzinfo=zone, fold=0).timestamp()
        if datetime.fromtimestamp(stamp, zone).replace(tzinfo=None) == local:
            slots.append(Slot(stamp, day.isoformat(), hhmm))
    return slots


def latest_slot(settings, now):
    zone = ZoneInfo(settings["timezone"])
    today = datetime.fromtimestamp(now, zone).date()
    previous = [
        slot
        for day in (today - timedelta(days=1), today)
        for slot in slots_on(settings, day, zone)
        if settings["changed_at"] < slot.stamp <= now
    ]
    return max(previous, default=None)


def slot_available(db, slot):
    seen = dict(db.execute("SELECT hhmm,queued FROM weather_slots WHERE day=?", (slot.day,)))
    return slot.hhmm not in seen and sum(seen.values()) < 4


def next_slot(db, settings, now):
    zone = ZoneInfo(settings["timezone"])
    today = datetime.fromtimestamp(now, zone).date()
    for days in range(15):
        for slot in slots_on(settings, today + timedelta(days=days), zone):
            if slot.stamp > max(now, settings["changed_at"]) and slot_available(db, slot):
                return datetime.fromtimestamp(slot.stamp, zone).strftime("%a %d %b %H:%M %Z")
    return "No eligible slot in the next two weeks"


@dataclass(frozen=True)
class Cache:
    attempted_at: float = 0
    next_at: float = 0
    expires: float | None = None
    modified: str = ""
    payload: str | None = None
    fetched_at: float = 0
    checked_at: float = 0
    status: str = ""
    deprecated: int = 0

    def forecast(self):
        return json.loads(self.payload) if self.payload else None


def cached(db, coordinates):
    row = db.execute(
        """SELECT attempted_at,next_at,expires,modified,payload,fetched_at,checked_at,status,deprecated
        FROM weather_cache WHERE coordinates=?""",
        (cache_key(coordinates),),
    ).fetchone()
    return Cache(*row) if row else Cache()


def cache_key(coordinates):
    return sha256(coordinates.encode("ascii")).hexdigest()


def cache_capacity(db, cache, now):
    if cache.next_at:
        return True
    return (
        db.execute(
            "SELECT COUNT(*) FROM weather_cache WHERE next_at>?",
            (now,),
        ).fetchone()[0]
        < CACHE_LIMIT
    )


def local_stamp(stamp, settings):
    return datetime.fromtimestamp(stamp, ZoneInfo(settings["timezone"])).strftime(
        "%a %d %b %H:%M %Z"
    )


def provider_status(db, settings, cache, now):
    if settings["throttle_until"] > now:
        return f"MET throttled requests (429) until {local_stamp(settings['throttle_until'], settings)}."
    if not cache_capacity(db, cache, now):
        return "Weather cache is waiting for an earlier coordinate's request gate to expire."
    if cache.deprecated:
        warning = "MET marked this endpoint deprecated (203). Review Provider setup."
        if cache.status != warning:
            return f"{warning} {cache.status}".strip()
    return cache.status or "No MET data requested for this location yet."


def reserve(db, settings, cache, now, jitter):
    if now < max(cache.next_at, settings["throttle_until"]):
        return False
    if not cache_capacity(db, cache, now):
        return False
    key = cache_key(settings["coordinates"])
    db.execute(
        """INSERT INTO weather_cache(coordinates,attempted_at,next_at,status) VALUES (?,?,?,?)
        ON CONFLICT(coordinates) DO UPDATE SET attempted_at=excluded.attempted_at,
            next_at=excluded.next_at""",
        (key, now, now + met.REQUEST_INTERVAL + jitter, "Waiting for MET response."),
    )
    # Evict only records whose retry and freshness gates have expired.
    db.execute(
        """DELETE FROM weather_cache WHERE coordinates IN (
            SELECT coordinates FROM weather_cache WHERE coordinates<>? AND next_at<=?
            ORDER BY attempted_at,coordinates
            LIMIT MAX(0,(SELECT COUNT(*) FROM weather_cache)-?))""",
        (key, now, CACHE_LIMIT),
    )
    return True


def save_response(db, coordinates, response, now, jitter, conditional):
    cache = cached(db, coordinates)
    expires = response.expires if response.expires is not None else cache.expires
    next_at = max(cache.next_at, (expires or 0) + jitter, response.retry_at)
    status = {
        0: "MET connection failed. Saved data may still be available.",
        200: "MET returned incomplete or unreadable data.",
        203: "MET marked this endpoint deprecated (203). Review Provider setup.",
        304: "MET returned 304 without a conditional forecast.",
        403: "MET refused access (403). Check the service contact in Provider setup.",
        429: "MET throttled requests (429).",
    }.get(response.status, f"MET request failed (HTTP {response.status}).")
    modified, payload, fetched_at = cache.modified, cache.payload, cache.fetched_at
    deprecated = cache.deprecated or response.status == 203
    if response.status == 200 and response.forecast:
        modified, payload, fetched_at = response.modified, json.dumps(response.forecast), now
        status, deprecated = "MET forecast received.", False
    elif response.status == 304 and conditional and payload:
        status = "MET confirmed the saved forecast (304)."
    db.execute(
        """UPDATE weather_cache SET next_at=?,expires=?,modified=?,payload=?,
        fetched_at=?,checked_at=?,status=?,deprecated=? WHERE coordinates=?""",
        (
            next_at,
            expires,
            modified,
            payload,
            fetched_at,
            now,
            status,
            int(deprecated),
            cache_key(coordinates),
        ),
    )
    if response.status == 429:
        db.execute(
            "UPDATE weather_settings SET throttle_until=MAX(throttle_until,?) WHERE id=1",
            (response.retry_at,),
        )


def forecast_card(settings, cache, now):
    return met.summarize(
        cache.forecast(),
        settings["label"],
        ZoneInfo(settings["timezone"]),
        now,
        cached=cache.fetched_at < now,
    )


def delivery_status(db, text):
    db.execute("UPDATE weather_settings SET delivery_status=? WHERE id=1", (text,))


class Weather:
    def __init__(self, store, outbox, owner, *, fetch=None, clock=None, jitter=None):
        self.store, self.outbox, self.owner = store, outbox, owner
        self.fetch = met.fetch if fetch is None else fetch
        self.clock = time.time if clock is None else clock
        self.jitter = (lambda: random.uniform(5, 120)) if jitter is None else jitter

    def run_once(self):
        now = self.clock()
        with self.store.connection() as db:
            settings = preferences(db)
            if settings["preview_until"] <= now and not self.due_slot(db, settings, now):
                return
            db.execute("BEGIN IMMEDIATE")
            settings = preferences(db)
            slot = self.due_slot(db, settings, now)
            slot = skip_paused_slot(db, slot, now)
            preview = settings["preview_until"] > now
            if not slot and not preview:
                return
            problem = access_problem(settings, self.owner) or guard_problem(db, now)
            if problem:
                self.finish(db, settings, slot, preview, now, problem)
                return
            cache = cached(db, settings["coordinates"])
            jitter = self.jitter()
            reserved = reserve(db, settings, cache, now, jitter)
            if not reserved:
                self.finish(db, settings, slot, preview, now)
                return

        coordinates = settings["coordinates"]
        modified_at = met.http_date(cache.modified)
        modified = cache.modified if modified_at is not None and modified_at <= now else ""
        response = self.fetch(coordinates, settings["contact"], modified, now)

        now = self.clock()
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            # Even a superseded location keeps its provider metadata and gates.
            save_response(db, coordinates, response, now, jitter, bool(modified))
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            current = preferences(db)
            if current["generation"] != settings["generation"]:
                return
            due = self.due_slot(db, current, now)
            due = skip_paused_slot(db, due, now)
            preview = current["preview_until"] > now
            problem = access_problem(current, self.owner) or guard_problem(db, now)
            self.finish(db, current, due, preview, now, problem)
            if slot and not due and now >= slot.stamp + SLOT_WINDOW:
                delivery_status(db, f"{slot.hhmm} skipped • the delivery window expired.")

    @staticmethod
    def due_slot(db, settings, now):
        if not settings["enabled"]:
            return None
        slot = latest_slot(settings, now)
        if not slot or now >= slot.stamp + SLOT_WINDOW or not slot_available(db, slot):
            return None
        return slot

    def finish(self, db, settings, slot, preview, now, problem=""):
        if not slot and not preview:
            return
        cache = cached(db, settings["coordinates"])
        forecast = None if problem else forecast_card(settings, cache, now)
        status = problem or provider_status(db, settings, cache, now)
        saved = cache.forecast()
        unusable = (
            "The saved forecast has expired."
            if saved and now >= met.forecast_expiry(saved)
            else "No usable forecast for the next 24 hours."
        )
        skip_reason = problem or f"{unusable} {status}"
        card = f"{forecast}\n\n{status}" if forecast else f"🌦 Weather\n{skip_reason}"
        usable_until = met.forecast_expiry(saved) if forecast else float("inf")
        if slot:
            record_slot(db, slot, bool(forecast))
            if forecast:
                db.execute("DELETE FROM outbox WHERE kind='weather'")
                expires = min(usable_until, slot.stamp + SLOT_WINDOW)
                self.enqueue(db, settings, card, "weather", now, expires, slot)
                delivery_status(db, f"{slot.hhmm} queued for Telegram.")
            else:
                delivery_status(db, f"{slot.hhmm} skipped • {skip_reason}")
        if preview:
            db.execute("UPDATE weather_settings SET preview_until=0 WHERE id=1")
            if not problem:
                self.enqueue(
                    db,
                    settings,
                    card,
                    "weather_preview",
                    now,
                    min(usable_until, settings["preview_until"]),
                )

    def enqueue(self, db, settings, text, kind, now, expires, slot=None):
        item = self.outbox.send(
            self.owner,
            text,
            db=db,
            now=now,
            kind=kind,
            reply_markup={"inline_keyboard": [met.attribution()]},
        )
        db.execute(
            "INSERT INTO weather_deliveries VALUES (?,?,?,?,?)",
            (
                item,
                settings["generation"],
                settings["coordinates"],
                expires,
                slot.stamp if slot else None,
            ),
        )


def record_slot(db, slot, queued):
    cutoff = date.fromisoformat(slot.day) - timedelta(days=8)
    db.execute("DELETE FROM weather_slots WHERE day<?", (cutoff.isoformat(),))
    db.execute("INSERT INTO weather_slots VALUES (?,?,?)", (slot.day, slot.hhmm, queued))


def skip_paused_slot(db, slot, now):
    if slot:
        pause = db.execute("SELECT value FROM state WHERE key='pause_until'").fetchone()
        if pause and slot.stamp < float(pause[0]) <= now:
            record_slot(db, slot, False)
            delivery_status(db, f"{slot.hhmm} skipped • global automation was paused.")
            return None
    return slot


def delivery_allowed(db, item, owner, now):
    row = db.execute(
        "SELECT generation,coordinates,expires,slot FROM weather_deliveries WHERE outbox_id=?",
        (item,),
    ).fetchone()
    settings = preferences(db)
    reason = access_problem(settings, owner) or guard_problem(db, now)
    if not row:
        db.execute("DELETE FROM outbox WHERE id=?", (item,))
        return False
    generation, coordinates, expires, slot = row
    if generation != settings["generation"] or coordinates != settings["coordinates"]:
        reason = "Weather settings changed."
    elif expires <= now:
        reason = "The delivery window expired."
    elif slot is not None:
        latest = latest_slot(settings, now)
        if not settings["enabled"]:
            reason = "Automatic weather is off."
        elif latest and latest.stamp > slot:
            reason = "A newer weather slot superseded it."
    if reason:
        if slot is not None:
            hhmm = datetime.fromtimestamp(slot, ZoneInfo(settings["timezone"])).strftime("%H:%M")
            delivery_status(db, f"{hhmm} skipped • {reason}")
        db.execute("DELETE FROM outbox WHERE id=?", (item,))
        return False
    return True


def delivered(db, item):
    row = db.execute(
        "SELECT slot FROM weather_deliveries WHERE outbox_id=? AND slot IS NOT NULL", (item,)
    ).fetchone()
    if row:
        hhmm = local_stamp(row[0], preferences(db))
        delivery_status(db, f"{hhmm} sent to Telegram.")


def pause_weather(db, now):
    if db.execute("SELECT 1 FROM outbox WHERE kind='weather'").fetchone():
        delivery_status(db, "Queued weather skipped • global automation changed.")
    db.execute(
        "UPDATE weather_settings SET preview_until=0, changed_at=MAX(changed_at,?) WHERE id=1",
        (now,),
    )
