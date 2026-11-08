"""Telegram controls, small background refresh batches and a combined briefing."""

import json
import logging
import threading
import time
from datetime import UTC, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from trabe.calendar import USCalendar
from trabe.ledger import Ledger
from trabe.portfolio import Portfolio
from trabe.signals import SignalHTTP, SignalStore, WorkStopped

logger = logging.getLogger(__name__)
FEATURES = ("earnings", "insiders", "news")


class Insights:
    def __init__(self, config, store, outbox, monitor=None, *, http_factory=SignalHTTP):
        self.config, self.store, self.outbox, self.monitor = config, store, outbox, monitor
        self.http_factory = http_factory
        self.repo = SignalStore(store)
        self.stop, self.wake = threading.Event(), threading.Event()
        self.worker = None

    def feature(self, kind, *, automatic=False, context=None, deadline=None, guard_settings=None):
        from trabe.earnings import Earnings
        from trabe.insiders import Insiders
        from trabe.news import News

        http = self.http_factory(
            self.config,
            self.store,
            stop=self.stop,
            automatic=automatic,
            feature=context or kind,
            deadline=deadline,
            guard_settings=guard_settings,
        )
        return {"earnings": Earnings, "insiders": Insiders, "news": News}[kind](self.store, http)

    def start(self):
        with self.store.connection() as db:
            db.execute("UPDATE signal_jobs SET status='pending' WHERE status='running'")
        self.worker = threading.Thread(target=self.run, name="fundamentals", daemon=True)
        self.worker.start()

    def market_context(self, *, automatic=False):
        from trabe.market_context import MarketContext

        return MarketContext(
            self.store,
            self.http_factory(
                self.config, self.store, stop=self.stop, automatic=automatic, feature="briefing"
            ),
        )

    def market_news(self, *, automatic=False):
        from trabe.market_news import MarketNews

        return MarketNews(
            self.store,
            self.http_factory(
                self.config, self.store, stop=self.stop, automatic=automatic, feature="briefing"
            ),
        )

    def close(self):
        self.stop.set()
        self.wake.set()
        if self.worker:
            self.worker.join(timeout=5)

    def queue(self, kind, symbols=None, *, origin="manual", gate=None, deadline=None, session=None):
        if kind not in (*FEATURES, "briefing"):
            raise ValueError("Unknown refresh type.")
        selected = sorted(Ledger(self.store).coverage() if symbols is None else set(symbols))
        if not selected:
            return (
                None
                if origin == "automatic"
                else "Add a watchlist symbol or record a position first."
            )
        held = Ledger(self.store).held()
        selected.sort(key=lambda ticker: (ticker not in held, ticker))
        settings = self.store.settings()
        encoded = json.dumps(
            {
                "all": selected,
                "remaining": selected,
                "origin": origin,
                "gate": gate,
                "deadline": deadline,
                "session": session,
                "settings": {k: settings[k] for k in ("snapshot_time", "timezone")}
                if origin == "automatic"
                else {},
            }
        )
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            active = db.execute(
                "SELECT kind,symbols FROM signal_jobs WHERE status IN ('pending','running')"
            ).fetchall()
            if origin == "automatic":
                existing = db.execute(
                    "SELECT id FROM signal_jobs WHERE kind=? AND json_extract(symbols,'$.origin')='automatic' AND json_extract(symbols,'$.session')=? AND json_extract(symbols,'$.gate')=? ORDER BY id DESC LIMIT 1",
                    (kind, session, gate),
                ).fetchone()
                if existing:
                    return existing[0]
            if any(
                k == kind
                and json.loads(s)["all"] == selected
                and json.loads(s).get("origin", "manual") == origin
                for k, s in active
            ):
                return "A matching refresh is already queued; /status remains available."
            if len(active) >= 16:
                return (
                    None
                    if origin == "automatic"
                    else "The refresh queue is full; cached views remain available."
                )
            cursor = db.execute(
                "INSERT INTO signal_jobs(kind,symbols,created_at) VALUES (?,?,?)",
                (kind, encoded, time.time()),
            )
        self.wake.set()
        if origin == "automatic":
            return cursor.lastrowid
        return f"{kind.capitalize()} refresh queued for {len(selected)} symbol(s). I'll send the result here."

    def handle(self, command, args):
        if command == "insider":
            command = "insiders"
        if command in FEATURES:
            if len(args) > 2 or (len(args) == 2 and args[0].lower() not in ("cached", "refresh")):
                raise ValueError(
                    f"Usage: /{command} [TICKER|cached [TICKER]|refresh [TICKER]|on|off]"
                )
            option = args[0].lower() if args else ""
            if command == "news" and option == "sources":
                return self.feature("news").sources_status(sorted(Ledger(self.store).coverage()))
            if option in ("on", "off"):
                if (
                    option == "on"
                    and command == "earnings"
                    and not (self.config.finnhub_key or self.config.api_key)
                ):
                    raise ValueError(
                        "Configure a Finnhub or Alpha Vantage key locally before enabling earnings."
                    )
                if option == "on" and command == "insiders" and not self.config.finnhub_key:
                    raise ValueError(
                        "A Finnhub key is required for non-derivative verification. Configure it locally and redeploy."
                    )
                with self.store.connection() as db:
                    db.execute("BEGIN IMMEDIATE")
                    previous = db.execute(
                        "SELECT value FROM settings WHERE key=?", (command + "_enabled",)
                    ).fetchone()[0]
                    db.execute(
                        "UPDATE settings SET value=? WHERE key=?", (option, command + "_enabled")
                    )
                    if command in ("insiders", "earnings") and option == "on" and previous != "on":
                        db.execute(
                            "INSERT OR REPLACE INTO state VALUES (?,?)",
                            (command + "_enabled_at", str(time.time())),
                        )
                    if option == "off":
                        db.execute("DELETE FROM outbox WHERE kind=?", (command,))
                self.wake.set()
                return f"{command.capitalize()} automation {option}. Cached views remain available; /{command} refresh requests new data."
            if option in ("cached", "refresh"):
                from trabe.bot import symbol

                symbols = [symbol(args[1])] if len(args) == 2 else None
                if option == "refresh":
                    return self.queue(command, symbols)
                if symbols is None:
                    symbols = sorted(Ledger(self.store).coverage())
                return self.feature(command).report(symbols, now=time.time())
            if args:
                from trabe.bot import symbol

                symbols = [symbol(args[0])]
            else:
                symbols = None
            return self.queue(command, symbols)
        if command == "briefing":
            if len(args) > 1:
                raise ValueError("Usage: /briefing [now|HH:MM|on|off]")
            option = args[0].lower() if args else ""
            if option == "now":
                return self.queue("briefing")
            if option in ("on", "off"):
                self.store.set_setting("briefing_enabled", option)
                if option == "off":
                    with self.store.connection() as db:
                        db.execute("DELETE FROM outbox WHERE kind='briefing'")
                self.wake.set()
            elif option:
                self.store.set_setting("briefing_time", option)
                self.store.set_setting("briefing_enabled", "on")
                self.wake.set()
            settings = self.store.settings()
            return (
                f"Briefing {settings['briefing_enabled']} • {settings['briefing_time']} ({settings['timezone']}) "
                "on the local date of each verified US session open, before market open. "
                "Change travel timezone with /set timezone AREA/CITY. "
                "An enabled briefing refreshes all sections, including those with standalone automation off. "
                "/briefing now requests a manual digest."
            )
        return None

    def status(self):
        settings = self.store.settings()
        lines = ["Earnings / insiders / news / briefing:"]
        for kind in (*FEATURES, "briefing"):
            statuses = self.repo.status(kind) if kind != "briefing" else []
            success = max((r["succeeded_at"] for r in statuses), default=0)
            stamp = datetime.fromtimestamp(success, UTC).isoformat() if success else "not refreshed"
            failures = sum(bool(r["error"]) for r in statuses)
            lines.append(
                f"{kind}: {settings[kind + '_enabled']} • {stamp} • source issues {failures}"
            )
        with self.store.connection() as db:
            jobs = db.execute(
                "SELECT id,kind,status,symbols FROM signal_jobs WHERE status IN ('pending','running') ORDER BY id"
            ).fetchall()
        pending = sum(row[2] == "pending" for row in jobs)
        running = sum(row[2] == "running" for row in jobs)
        lines.append(
            f"Queued refreshes: {pending} • running: {running} • {self.store.state('insights_error') or 'worker ready'}"
        )
        for job, kind, status, encoded in jobs[:3]:
            payload = json.loads(encoded)
            total, remaining = len(payload["all"]), len(payload["remaining"])
            current = (
                ", checking " + payload["remaining"][0] if status == "running" and remaining else ""
            )
            lines.append(
                f"{kind.capitalize()} #{job}: {total - remaining}/{total} checked{current}"
            )
        return "\n".join(lines)

    @staticmethod
    def bounded(text):
        if len(text) <= 13000:
            return text
        return (
            text[:12500].rsplit("\n", 1)[0]
            + "\nMore data is cached; request a specific ticker for details."
        )

    def render_briefing(self, symbols, now):
        settings = self.store.settings()
        stamp = datetime.fromtimestamp(now, ZoneInfo(settings["timezone"])).isoformat(
            timespec="seconds"
        )
        lines = [f"🗞 trabe briefing • {stamp} ({settings['timezone']})"]
        try:
            lines.append("US market: " + USCalendar().describe(now, settings["timezone"]))
        except ValueError:
            lines.append("US market: calendar unavailable/expired")
        lines.extend(("", *self.market_context().briefing(symbols, now=now)))
        lines.extend(("", *self.market_news().briefing(now=now)))
        portfolio = Portfolio(self.store, self.monitor)
        currency, _, rows = portfolio.calculate(now=now)
        total = sum((r["value"] for r in rows if r["value"] is not None), Decimal(0))
        profit = sum((r["profit"] for r in rows if r["profit"] is not None), Decimal(0))
        partial = any(r["value"] is None or r["profit"] is None for r in rows)
        lines.extend(
            (
                "",
                "Positions",
                f"{len(rows)} open • observed value {total:.2f} {currency} • unrealized {profit:+.2f} {currency}"
                + (" • partial; missing values excluded" if partial else ""),
            )
        )
        for row in sorted(rows, key=lambda r: r["value"] or Decimal(0), reverse=True)[:3]:
            value = "unavailable" if row["value"] is None else f"{row['value']:.2f} {currency}"
            quote = row["quote"]
            quality = (
                "no price" if not quote else f"price age {max(0, int(now - quote['source_time']))}s"
            )
            lines.append(f"• {row['ticker']}: {row['quantity']} shares • {value} • {quality}")
        lines.extend(("", "Watchlist prices (cached observations)"))
        provider = settings["market_provider"]
        alpha_cache = self.store.all_cached_quotes() if provider == "alphavantage" else {}
        for ticker in symbols[:8]:
            if provider == "finnhub":
                quote = self.monitor.latest(ticker) if self.monitor else None
                quote = quote or self.store.cached_live("finnhub", ticker)
            else:
                quote = alpha_cache.get(ticker, (None,))[0]
            if not quote:
                lines.append(f"• {ticker}: no cached {provider} price")
            elif provider == "finnhub":
                observed = datetime.fromtimestamp(quote["source_time"], UTC).isoformat(
                    timespec="seconds"
                )
                lines.append(f"• {ticker}: {quote['price']:.4f} USD • Finnhub • {observed}")
            else:
                lines.append(
                    f"• {ticker}: {quote['price']:.4f} • Alpha Vantage end-of-day {quote['trading_day']} • listing currency"
                )
        for kind, title in (
            ("earnings", "Earnings"),
            ("insiders", "Insider purchases"),
            ("news", "News"),
        ):
            lines.extend(("", title))
            items = self.feature(kind).briefing(symbols, now=now, limit=3)
            lines.extend(items or ["No eligible cached items; source coverage may be unavailable."])
            statuses = self.repo.status(kind)
            relevant = [s for s in statuses if s["symbol"] in symbols or s["symbol"] == "*"]
            if not relevant:
                lines.append("Source has not been refreshed yet.")
            elif any(s["error"] for s in relevant):
                lines.append(
                    "Some source requests failed; retained data may be stale. Use the section command for details."
                )
        lines.append("\nCached observations; local sentiment is heuristic.")
        from trabe.snapshots import SnapshotService

        lines.append(SnapshotService(self.store).status())
        return self.bounded("\n".join(lines))

    def _coherent(self, db, settings, symbols, watermarks):
        if dict(db.execute("SELECT key,value FROM settings")) != settings:
            return False
        if Ledger._held(db) | {r[0] for r in db.execute("SELECT ticker FROM watchlist")} != set(
            symbols
        ):
            return False
        current = dict(
            db.execute(
                "SELECT key,value FROM state WHERE key IN ('insiders_enabled_at','earnings_enabled_at','insights_resumed_at','pause_until')"
            )
        )
        return current == watermarks

    def alert(self, kind, symbols, now):
        settings = self.store.settings()
        if settings[kind + "_enabled"] != "on":
            return
        with self.store.connection() as db:
            watermarks = dict(
                db.execute(
                    "SELECT key,value FROM state WHERE key IN ('insiders_enabled_at','earnings_enabled_at','insights_resumed_at','pause_until')"
                )
            )
        for key, message in self.feature(kind).alerts(symbols, now=now):
            self.repo.notify_once(
                self.config.owner_id,
                kind,
                key,
                self.bounded(message),
                now=now,
                setting=kind + "_enabled",
                validator=lambda db: self._coherent(db, settings, symbols, watermarks),
            )

    def scheduled_briefing(self, symbols, now):
        settings = self.store.settings()
        if settings["briefing_enabled"] != "on" or self.store.automation_paused(now):
            return
        try:
            session = USCalendar().next_session(now)
            zone = ZoneInfo(settings["timezone"])
            local_day = datetime.fromtimestamp(session.opens, zone).date()
            scheduled = datetime.combine(
                local_day, datetime.strptime(settings["briefing_time"], "%H:%M").time(), zone
            ).timestamp()
            if not scheduled <= now < session.opens:
                return
        except ValueError:
            self.store.set_state("insights_error", "Briefing suspended: exchange calendar expired.")
            return
        # Claims follow the exchange session even across local midnight or travel.
        key = "daily:" + session.day
        if self.repo.notified("briefing", key):
            return
        missing = any(
            not set(symbols).issubset(
                {r["symbol"] for r in self.repo.status(kind) if r["attempted_at"]}
            )
            for kind in FEATURES
        )
        waiting_key = "briefing_wait_" + session.day
        began = float(self.store.state(waiting_key, "0"))
        if missing and (not began or now - began < 300):
            if not began:
                self.store.set_state(waiting_key, str(now))
            return
        # Incomplete sections remain explicit instead of inventing freshness.
        text = self.render_briefing(symbols, now)
        with self.store.connection() as db:
            watermarks = dict(
                db.execute(
                    "SELECT key,value FROM state WHERE key IN ('insiders_enabled_at','earnings_enabled_at','insights_resumed_at','pause_until')"
                )
            )
        if self.repo.notify_once(
            self.config.owner_id,
            "briefing",
            key,
            text,
            now=now,
            setting="briefing_enabled",
            validator=lambda db: self._coherent(db, settings, symbols, watermarks),
        ):
            self.repo.put(
                "briefing",
                key,
                "",
                "trabe",
                {"as_of": now, "text": text, "symbols": symbols},
                now=now,
                event_time=now,
            )

    def process_job(self):
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT id,kind,symbols FROM signal_jobs WHERE status='pending' ORDER BY id LIMIT 1"
            ).fetchone()
            if not row:
                return False
            db.execute("UPDATE signal_jobs SET status='running' WHERE id=?", (row[0],))
        job, kind, encoded = row
        payload = json.loads(encoded)
        automatic = payload.get("origin") == "automatic"
        gate = payload.get("gate")
        deadline = payload.get("deadline")

        def cancelled():
            current = self.store.settings()
            return automatic and (
                self.store.automation_paused()
                or self.store.state("storage_paused")
                or current.get(str(gate) + "_enabled") != "on"
                or (deadline is not None and time.time() >= deadline)
                or any(current.get(k) != v for k, v in payload.get("settings", {}).items())
            )

        if cancelled():
            with self.store.connection() as db:
                db.execute("UPDATE signal_jobs SET status='cancelled' WHERE id=?", (job,))
            return True
        if automatic:
            covered = Ledger(self.store).coverage()
            payload["remaining"] = [s for s in payload["remaining"] if s in covered]
        batch_size = 1 if kind in {"news", "briefing"} else 5
        batch, remaining = payload["remaining"][:batch_size], payload["remaining"][batch_size:]
        try:
            if kind == "briefing":
                if payload["remaining"] == payload["all"]:
                    self.market_news(automatic=automatic).refresh(
                        now=time.time(), force=not automatic
                    )
                self.market_context(automatic=automatic).refresh(
                    batch,
                    now=time.time(),
                    force=not automatic,
                    include_market=payload["remaining"] == payload["all"],
                )
            for feature in FEATURES if kind == "briefing" else (kind,):
                self.feature(
                    feature,
                    automatic=automatic,
                    context=gate,
                    deadline=deadline,
                    guard_settings=payload.get("settings", {}),
                ).refresh(batch, now=time.time(), force=not automatic)
            if cancelled():
                raise WorkStopped("Automatic preparation cancelled.")
            if remaining:
                payload["remaining"] = remaining
                with self.store.connection() as db:
                    db.execute(
                        "UPDATE signal_jobs SET status='pending',symbols=? WHERE id=? AND status='running'",
                        (json.dumps(payload), job),
                    )
            else:
                payload["remaining"] = []
                if automatic:
                    with self.store.connection() as db:
                        db.execute(
                            "UPDATE signal_jobs SET status='completed',symbols=? WHERE id=? AND status='running'",
                            (json.dumps(payload), job),
                        )
                    return True
                text = (
                    self.render_briefing(payload["all"], time.time())
                    if kind == "briefing"
                    else self.feature(kind).report(payload["all"], now=time.time())
                )
                with self.store.connection() as db:
                    db.execute("BEGIN IMMEDIATE")
                    self.outbox.send(self.config.owner_id, self.bounded(text), db=db)
                    db.execute(
                        "UPDATE signal_jobs SET status='completed',symbols=? WHERE id=?",
                        (json.dumps(payload), job),
                    )
        except WorkStopped:
            with self.store.connection() as db:
                db.execute(
                    "UPDATE signal_jobs SET status=? WHERE id=?",
                    ("cancelled" if automatic else "pending", job),
                )
        except Exception:
            logger.warning("Manual signal refresh failed; cached data retained.")
            with self.store.connection() as db:
                db.execute("BEGIN IMMEDIATE")
                if not automatic:
                    self.outbox.send(
                        self.config.owner_id,
                        f"{kind.capitalize()} refresh was incomplete. Cached data is retained; check /status and retry.",
                        db=db,
                    )
                db.execute("UPDATE signal_jobs SET status='failed' WHERE id=?", (job,))
        return True

    def run_once(self, now=None):
        fixed_time = now is not None
        now = time.time() if now is None else now
        if self.process_job():
            return True
        settings = self.store.settings()
        if self.store.automation_paused(now) or self.store.state("storage_paused"):
            return False
        held = Ledger(self.store).held()
        symbols = sorted(held) + sorted(Ledger(self.store).coverage() - held)
        if not symbols:
            return False
        if settings["briefing_enabled"] == "on":
            offset = int(self.store.state("signal_cursor_market-context", "0")) % len(symbols)
            try:
                self.market_news(automatic=True).refresh(now=now if fixed_time else time.time())
                self.market_context(automatic=True).refresh(
                    [symbols[offset]], now=now if fixed_time else time.time()
                )
            except WorkStopped:
                if self.stop.is_set() or self.store.automation_paused():
                    raise
            self.store.set_state("signal_cursor_market-context", str((offset + 1) % len(symbols)))
        for kind in FEATURES:
            own = settings[kind + "_enabled"] == "on"
            if not own and settings["briefing_enabled"] != "on":
                continue
            offset = int(self.store.state("signal_cursor_" + kind, "0")) % len(symbols)
            batch = (symbols + symbols)[offset : offset + min(5, len(symbols))]
            try:
                self.feature(kind, automatic=True, context=kind if own else "briefing").refresh(
                    batch, now=now if fixed_time else time.time()
                )
            except WorkStopped:
                if self.stop.is_set() or self.store.automation_paused():
                    raise
                continue
            except Exception:
                self.store.set_state(
                    "insights_error", f"{kind.capitalize()} refresh failed; other sources continue."
                )
                continue
            self.store.set_state("signal_cursor_" + kind, str((offset + len(batch)) % len(symbols)))
            self.alert(kind, symbols, now if fixed_time else time.time())
        self.scheduled_briefing(symbols, now if fixed_time else time.time())
        return False

    def run(self):
        while not self.stop.is_set():
            self.wake.clear()
            immediate = False
            try:
                self.store.set_state("insights_error", "")
                immediate = self.run_once()
            except WorkStopped:
                pass
            except Exception:
                logger.warning("Signal background work failed; retry scheduled.")
                self.store.set_state(
                    "insights_error", "Background source work failed; cached views retained."
                )
            self.wake.wait(0.2 if immediate else 30)
