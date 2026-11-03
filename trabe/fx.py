"""Daily Frankfurter/ECB reference rates, fetched only by a background worker."""

import json
import time
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from urllib.error import URLError
from urllib.request import Request, urlopen

from trabe.ledger import Ledger


class FX:
    def __init__(self, store, opener=urlopen):
        self.store = store
        self.opener = opener

    def fetch(self, day, now=None):
        now = time.time() if now is None else now
        # Historical rates are final only after the requested UTC day has ended.
        end_of_day = datetime.combine(
            date.fromisoformat(day) + timedelta(days=1), datetime.min.time(), UTC
        ).timestamp()
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            cached = db.execute(
                "SELECT retrieved_at FROM fx_cache WHERE requested_date=?", (day,)
            ).fetchone()
            if now >= end_of_day and cached and cached[0] >= end_of_day:
                return
            # Each requested date has a shared retry budget, including outages.
            row = db.execute(
                "SELECT attempted_at FROM fx_attempts WHERE requested_date=?", (day,)
            ).fetchone()
            if row and now - row[0] < 3600:
                return
            db.execute("INSERT OR REPLACE INTO fx_attempts VALUES (?,?)", (day, now))
        url = f"https://api.frankfurter.dev/v1/{day}?base=EUR&symbols=USD"
        try:
            request = Request(url, headers={"User-Agent": "trabe/0.4"})
            with self.opener(request, timeout=10) as response:
                payload = json.loads(response.read(65536), parse_float=Decimal)
            rate = Decimal(str(payload["rates"]["USD"]))
            effective = date.fromisoformat(payload["date"])
            requested = date.fromisoformat(day)
            if (
                payload["base"] != "EUR"
                or not rate.is_finite()
                or rate <= 0
                or not 0 <= (requested - effective).days <= 7
            ):
                return
        except (OSError, URLError, ValueError, KeyError, TypeError, InvalidOperation):
            return
        with self.store.connection() as db:
            db.execute(
                "INSERT OR REPLACE INTO fx_cache VALUES (?,?,?,?,?)",
                (day, str(rate), effective.isoformat(), "Frankfurter/ECB", now),
            )

    def refresh(self, now=None):
        now = time.time() if now is None else now
        today = datetime.fromtimestamp(now, UTC).date().isoformat()
        with self.store.connection() as db:
            days = [
                row[0]
                for row in db.execute(
                    "SELECT DISTINCT t.trade_date FROM trades t "
                    "LEFT JOIN fx_attempts a ON a.requested_date=t.trade_date "
                    "WHERE t.fx_rate IS NULL AND t.voided_at IS NULL AND t.side != 'adjust' "
                    "AND t.trade_date < ? AND (a.attempted_at IS NULL OR a.attempted_at<=?) "
                    "ORDER BY t.trade_date LIMIT 3",
                    (today, now - 3600),
                )
            ]
        requested = ([today] if Ledger(self.store).held() else []) + days
        for day in requested:
            if self.store.automation_paused():
                break
            self.fetch(day, now)
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute(
                """SELECT t.id,t.currency,c.usd_per_eur,c.effective_date,c.provider,c.retrieved_at
                FROM trades t JOIN fx_cache c ON c.requested_date=t.trade_date
                WHERE t.fx_rate IS NULL AND t.trade_date < ?
                AND c.retrieved_at >= CAST(strftime('%s', t.trade_date, '+1 day') AS INTEGER)""",
                (today,),
            ).fetchall()
            for trade_id, currency, rate, effective, provider, fetched in rows:
                rate = Decimal(rate)
                # Persist original currency -> other currency, with explicit direction inferred from original.
                converted = rate if currency == "EUR" else Decimal(1) / rate
                db.execute(
                    "UPDATE trades SET fx_rate=?,fx_date=?,fx_provider=?,fx_retrieved=?,fx_from=?,fx_to=? WHERE id=? AND fx_rate IS NULL",
                    (
                        str(converted),
                        effective,
                        provider,
                        str(fetched),
                        currency,
                        "USD" if currency == "EUR" else "EUR",
                        trade_id,
                    ),
                )

    def current(self, now=None):
        now = time.time() if now is None else now
        today = datetime.fromtimestamp(now, UTC).date()
        with self.store.connection() as db:
            row = db.execute(
                "SELECT usd_per_eur,effective_date,provider,retrieved_at FROM fx_cache WHERE requested_date<=? ORDER BY requested_date DESC LIMIT 1",
                (today.isoformat(),),
            ).fetchone()
        if not row or not 0 <= (today - date.fromisoformat(row[1])).days <= 7:
            return None
        return Decimal(row[0]), row[1], row[2], row[3]
