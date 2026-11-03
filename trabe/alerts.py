"""Pure eligibility decisions and atomic event/observation/reply persistence."""

import math
import time
from datetime import UTC, datetime

from trabe.outbox import Outbox


def breach(price: float, previous_close: float, threshold: float) -> float | None:
    if not all(math.isfinite(v) for v in (price, previous_close, threshold)):
        return None
    if price <= 0 or previous_close <= 0:
        return None
    change = (price / previous_close - 1) * 100
    return change if change <= threshold else None


class Alerts:
    def __init__(self, store, owner_id: int):
        self.store, self.owner_id = store, owner_id

    def evaluate(
        self, ticker: str, quote: dict, bucket: dict, baseline: dict, *, now: float | None = None
    ) -> bool:
        now = time.time() if now is None else now
        settings = self.store.settings()
        if not baseline or not baseline["accepted"]:
            return False
        if (
            quote["provider"] != "finnhub"
            or baseline["provider"] != quote["provider"]
            or quote.get("ticker") != ticker
            or quote.get("feed") != "us-trades"
            or quote.get("currency") != "USD"
            or quote.get("price_kind") != "last-trade"
            or quote.get("previous_close") != baseline["previous_close"]
        ):
            return False
        if baseline["session_date"] != bucket["session_date"]:
            return False
        age = now - quote["source_time"]
        if not -2 <= age <= int(settings["max_source_age_seconds"]):
            return False
        enabled, threshold = self.store.alert_rule(ticker)
        if not enabled:
            return False
        change = breach(quote["price"], baseline["previous_close"], threshold)
        if change is None:
            return False
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            current = dict(db.execute("SELECT key,value FROM settings"))
            pause = db.execute("SELECT value FROM state WHERE key='pause_until'").fetchone()
            if current["monitoring"] != "on" or current["market_provider"] != "finnhub":
                return False
            if pause and float(pause[0]) > now:
                return False
            storage = db.execute("SELECT value FROM state WHERE key='storage_paused'").fetchone()
            if storage and storage[0]:
                return False
            if not -2 <= age <= int(current["max_source_age_seconds"]):
                return False
            if abs(baseline["open_price"] / baseline["previous_close"] - 1) * 100 > float(
                current["baseline_max_gap_percent"]
            ):
                return False
            saved = db.execute(
                "SELECT accepted,previous_close FROM baselines WHERE ticker=? AND provider=? AND session_date=?",
                (ticker, quote["provider"], bucket["session_date"]),
            ).fetchone()
            if not saved or not saved[0] or saved[1] != baseline["previous_close"]:
                return False
            preference = db.execute(
                "SELECT enabled,threshold FROM alert_preferences WHERE ticker=?", (ticker,)
            ).fetchone()
            # A concurrent preference edit must take effect before the claim commits.
            if preference and not preference[0]:
                return False
            threshold = (
                preference[1]
                if preference and preference[1] is not None
                else float(current["dip_threshold_percent"])
            )
            change = breach(quote["price"], baseline["previous_close"], threshold)
            if (
                change is None
                or not db.execute("SELECT 1 FROM watchlist WHERE ticker=?", (ticker,)).fetchone()
            ):
                return False
            session = db.execute(
                "SELECT opens,closes,finalized FROM market_sessions WHERE session_date=?",
                (bucket["session_date"],),
            ).fetchone()
            if not session or session[2] or not session[0] <= quote["source_time"] < session[1]:
                return False
            if not session[0] <= now < session[1]:
                return False
            claimed = db.execute(
                "INSERT OR IGNORE INTO alert_events(ticker,rule,session_date,price,previous_close,change_percent,threshold,source_ms,provider,feed,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    ticker,
                    "previous-close",
                    bucket["session_date"],
                    quote["price"],
                    baseline["previous_close"],
                    change,
                    threshold,
                    int(quote["source_time"] * 1000),
                    quote["provider"],
                    quote["feed"],
                    now,
                ),
            ).rowcount
            if not claimed:
                return False
            self.store.save_bucket(bucket, db)
            self.store.save_live(quote, db)
            source = datetime.fromtimestamp(quote["source_time"], UTC).isoformat()
            Outbox(self.store).send(
                self.owner_id,
                f"🔻 trabe dip alert • {ticker}\n{quote['price']:.4f} USD • {change:+.2f}% vs previous close\n"
                f"Threshold: {threshold:g}% • session {bucket['session_date']}\n"
                f"Baseline: {baseline['previous_close']:.4f} ({baseline['previous_date']})\n"
                f"Source: Finnhub / {quote['feed']} • {source}\n"
                "Observed feed price; one alert per symbol/rule/session.",
                kind="alert",
                db=db,
            )
            return True
