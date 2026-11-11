"""Immutable simulated instructions/fills; balances are derived by Decimal replay."""

import copy
import uuid
from datetime import UTC, date, datetime
from decimal import ROUND_FLOOR, ROUND_HALF_UP, Decimal, localcontext

from trabe.calendar import USCalendar
from trabe.market_context import SOURCE, number
from trabe.outlook import digest, encoded
from trabe.paper_rules import PARAMETERS, classify, whole
from trabe.price_endpoints import METHOD, checked, enabled

MAX_DECISIONS = 10000
MAX_RUN_BYTES = 16 * 1024 * 1024
MAX_PACKET = 16384

PAPER_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS paper_runs(id TEXT PRIMARY KEY,created_at REAL NOT NULL,
        config TEXT NOT NULL,config_sha256 TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS paper_run_state(run_id TEXT PRIMARY KEY REFERENCES paper_runs(id),
        status TEXT NOT NULL CHECK(status IN ('active','paused','closed')))""",
    "CREATE UNIQUE INDEX IF NOT EXISTS paper_one_unfinished ON paper_run_state((1)) WHERE status!='closed'",
    """CREATE TABLE IF NOT EXISTS paper_batches(id TEXT PRIMARY KEY,run_id TEXT NOT NULL REFERENCES paper_runs(id),
        session_date TEXT NOT NULL,scheduled_at REAL NOT NULL,decided_at REAL NOT NULL,
        opens REAL NOT NULL,closes REAL NOT NULL,payload TEXT NOT NULL,sha256 TEXT NOT NULL,
        UNIQUE(run_id,session_date))""",
    """CREATE TABLE IF NOT EXISTS paper_decisions(id TEXT PRIMARY KEY,batch_id TEXT NOT NULL REFERENCES paper_batches(id),
        ticker TEXT NOT NULL,payload TEXT NOT NULL,sha256 TEXT NOT NULL,UNIQUE(batch_id,ticker))""",
    """CREATE TABLE IF NOT EXISTS paper_orders(decision_id TEXT PRIMARY KEY REFERENCES paper_decisions(id),
        side TEXT NOT NULL CHECK(side IN ('buy','sell')),
        status TEXT NOT NULL CHECK(status IN ('pending','filled','cancelled','expired')),
        reserved_usd TEXT NOT NULL,expires_at REAL NOT NULL,last_error TEXT NOT NULL DEFAULT '')""",
    "CREATE INDEX IF NOT EXISTS paper_orders_due ON paper_orders(status,expires_at)",
    """CREATE TABLE IF NOT EXISTS paper_fills(id TEXT PRIMARY KEY,decision_id TEXT NOT NULL UNIQUE REFERENCES paper_decisions(id),
        run_id TEXT NOT NULL REFERENCES paper_runs(id),ticker TEXT NOT NULL,session_date TEXT NOT NULL,
        side TEXT NOT NULL CHECK(side IN ('buy','sell')),
        quantity INTEGER NOT NULL CHECK(typeof(quantity)='integer' AND quantity>0),
        price_usd TEXT NOT NULL,fee_eur TEXT NOT NULL,fee_usd TEXT NOT NULL,
        executed_at REAL NOT NULL,recorded_at REAL NOT NULL,payload TEXT NOT NULL,sha256 TEXT NOT NULL,
        UNIQUE(session_date,ticker))""",
    "CREATE INDEX IF NOT EXISTS paper_fills_replay ON paper_fills(run_id,executed_at,id)",
    """CREATE TABLE IF NOT EXISTS paper_session_state(run_id TEXT NOT NULL REFERENCES paper_runs(id),
        session_date TEXT NOT NULL,state TEXT NOT NULL CHECK(state IN ('preparing','captured','missed','cancelled')),
        reason TEXT NOT NULL DEFAULT '',PRIMARY KEY(run_id,session_date))""",
    """CREATE TABLE IF NOT EXISTS fx_observations(id TEXT PRIMARY KEY,requested_date TEXT NOT NULL,
        effective_date TEXT NOT NULL,known_at REAL NOT NULL,usd_per_eur TEXT NOT NULL,provider TEXT NOT NULL,
        payload TEXT NOT NULL,sha256 TEXT NOT NULL)""",
    "CREATE INDEX IF NOT EXISTS paper_fx_selection ON fx_observations(known_at,id)",
    """CREATE TABLE IF NOT EXISTS paper_action_conflicts(
        run_id TEXT NOT NULL REFERENCES paper_runs(id),ticker TEXT NOT NULL,
        detected_at REAL NOT NULL,payload TEXT NOT NULL,sha256 TEXT NOT NULL,
        PRIMARY KEY(run_id,ticker))""",
    # Work/mark references preserve endpoints until a fill has copied the consumed observation.
    """CREATE TABLE IF NOT EXISTS paper_price_inputs(
        symbol TEXT NOT NULL,session_date TEXT NOT NULL,edge TEXT NOT NULL CHECK(edge IN ('open','close')),
        ready_at REAL NOT NULL,expires_at REAL NOT NULL,
        endpoint_id TEXT REFERENCES market_price_endpoints(id) ON DELETE RESTRICT,
        PRIMARY KEY(symbol,session_date,edge))""",
) + tuple(
    statement
    for table, conflict in (
        ("paper_runs", "id=NEW.id"),
        ("paper_batches", "id=NEW.id OR (run_id=NEW.run_id AND session_date=NEW.session_date)"),
        ("paper_decisions", "id=NEW.id OR (batch_id=NEW.batch_id AND ticker=NEW.ticker)"),
        (
            "paper_fills",
            "id=NEW.id OR decision_id=NEW.decision_id OR (session_date=NEW.session_date AND ticker=NEW.ticker)",
        ),
        ("fx_observations", "id=NEW.id"),
        ("paper_action_conflicts", "run_id=NEW.run_id AND ticker=NEW.ticker"),
    )
    for statement in (
        f"CREATE TRIGGER IF NOT EXISTS immutable_{table} BEFORE UPDATE ON {table} BEGIN SELECT RAISE(ABORT,'Paper evidence is immutable'); END",
        f"CREATE TRIGGER IF NOT EXISTS no_replace_{table} BEFORE INSERT ON {table} WHEN EXISTS(SELECT 1 FROM {table} WHERE {conflict}) BEGIN SELECT RAISE(ABORT,'Paper evidence cannot be replaced'); END",
    )
)


def money(value):
    with localcontext() as ctx:
        ctx.prec = 28
        value = Decimal(value)
        if not value.is_finite() or abs(value) > Decimal("1e18"):
            raise ValueError("Invalid paper money amount.")
        return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


class PaperStore:
    def __init__(self, store):
        self.store = store

    def current(self, db):
        row = db.execute(
            "SELECT r.id,r.created_at,r.config,r.config_sha256,s.status FROM paper_runs r JOIN paper_run_state s ON s.run_id=r.id WHERE s.status!='closed'"
        ).fetchone()
        if not row:
            return None
        return {"id": row[0], "created_at": row[1], "config": checked(row[2:4]), "status": row[4]}

    def start(self, *, now, calendar_version):
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = self.current(db)
            if existing:
                return existing
            config = copy.deepcopy(PARAMETERS)
            config.update(
                calendar_version=calendar_version,
                insider_min_value_usd=self.store.settings(db)["insider_min_value_usd"],
            )
            run_id = uuid.uuid4().hex[:16]
            db.execute(
                "INSERT INTO paper_runs VALUES (?,?,?,?)",
                (run_id, now, encoded(config), digest(config)),
            )
            db.execute("INSERT INTO paper_run_state VALUES (?,'active')", (run_id,))
            db.execute("UPDATE settings SET value='on' WHERE key='paper_enabled'")
            return self.current(db)

    @staticmethod
    def cancel_pending(db, reason, *, run_id=None, status="cancelled"):
        db.execute(
            "UPDATE paper_orders SET status=?,reserved_usd='0.00',last_error=? WHERE status='pending'"
            + (
                " AND decision_id IN (SELECT d.id FROM paper_decisions d JOIN paper_batches b ON b.id=d.batch_id WHERE b.run_id=?)"
                if run_id
                else ""
            ),
            (status, reason, run_id) if run_id else (status, reason),
        )

    @staticmethod
    def finish_order(db, decision_id, reason, status="cancelled"):
        db.execute(
            "UPDATE paper_orders SET status=?,reserved_usd='0.00',last_error=? WHERE decision_id=? AND status='pending'",
            (status, reason, decision_id),
        )

    def replay(self, db, run_id):
        row = db.execute(
            "SELECT config,config_sha256 FROM paper_runs WHERE id=?", (run_id,)
        ).fetchone()
        if not row:
            raise ValueError("Unknown paper run.")
        config = checked(row)
        cash, fees, realized = money(config["initial_cash_usd"]), money(0), money(0)
        positions = {}
        with localcontext() as ctx:
            ctx.prec = 28
            for row in db.execute(
                "SELECT payload,sha256 FROM paper_fills WHERE run_id=? ORDER BY executed_at,id",
                (run_id,),
            ):
                value = checked(row)
                qty = whole(value["quantity"])
                if qty <= 0:
                    raise ValueError("Invalid saved paper quantity.")
                position = positions.setdefault(value["ticker"], {"quantity": 0, "basis": money(0)})
                price, fee = number(value["price_usd"]), money(value["fee_usd"])
                gross = money(qty * price)
                if value["side"] == "buy":
                    cash -= gross + fee
                    position["quantity"] += qty
                    position["basis"] += gross + fee
                else:
                    owned = whole(position["quantity"])
                    if qty > owned:
                        raise ValueError("Saved paper sale exceeds ownership.")
                    basis = (
                        position["basis"]
                        if qty == owned
                        else money(position["basis"] * qty / owned)
                    )
                    cash += gross - fee
                    realized += gross - fee - basis
                    position["quantity"] -= qty
                    position["basis"] -= basis
                fees += fee
                if cash < 0:
                    raise ValueError("Saved paper cash is negative.")
        reserved = sum(
            (
                money(r[0])
                for r in db.execute(
                    "SELECT o.reserved_usd FROM paper_orders o JOIN paper_decisions d ON d.id=o.decision_id JOIN paper_batches b ON b.id=d.batch_id WHERE b.run_id=? AND o.status='pending'",
                    (run_id,),
                )
            ),
            money(0),
        )
        return {
            "cash": cash,
            "reserved": reserved,
            "fees": fees,
            "realized": realized,
            "positions": positions,
        }

    @staticmethod
    def fx(db, when):
        day = datetime.fromtimestamp(when, UTC).date()
        for row in db.execute(
            "SELECT payload,sha256 FROM fx_observations WHERE known_at<=? ORDER BY known_at DESC,id DESC",
            (when,),
        ):
            value = checked(row)
            try:
                if (
                    value["provider"] == "Frankfurter/ECB"
                    and value["known_at"] <= when
                    and date.fromisoformat(value["requested_date"]) <= day
                    and 0 <= (day - date.fromisoformat(value["effective_date"])).days <= 7
                ):
                    number(value["usd_per_eur"])
                    return value
            except (ValueError, KeyError, TypeError):
                continue
        return None

    @staticmethod
    def observe_fx(db, day, rate, effective, provider, known_at):
        rate = number(rate)
        if (
            provider != "Frankfurter/ECB"
            or not 0 <= (date.fromisoformat(day) - date.fromisoformat(effective)).days <= 7
        ):
            raise ValueError("Invalid ECB observation.")
        value = {
            "requested_date": day,
            "effective_date": effective,
            "known_at": known_at,
            "usd_per_eur": str(rate),
            "provider": provider,
        }
        value["id"] = digest(value)[:32]
        if not db.execute("SELECT 1 FROM fx_observations WHERE id=?", (value["id"],)).fetchone():
            db.execute(
                "INSERT INTO fx_observations VALUES (?,?,?,?,?,?,?,?)",
                (
                    value["id"],
                    day,
                    effective,
                    known_at,
                    str(rate),
                    provider,
                    encoded(value),
                    digest(value),
                ),
            )
        return value

    @staticmethod
    def size(db, run_id):
        return db.execute(
            "SELECT (SELECT length(CAST(config AS BLOB)) FROM paper_runs WHERE id=?) + COALESCE((SELECT SUM(length(CAST(payload AS BLOB))) FROM paper_batches WHERE run_id=?),0) + COALESCE((SELECT SUM(length(CAST(d.payload AS BLOB))) FROM paper_decisions d JOIN paper_batches b ON b.id=d.batch_id WHERE b.run_id=?),0) + COALESCE((SELECT SUM(length(CAST(payload AS BLOB))) FROM paper_fills WHERE run_id=?),0) + COALESCE((SELECT SUM(length(CAST(payload AS BLOB))) FROM paper_action_conflicts WHERE run_id=?),0)",
            (run_id,) * 5,
        ).fetchone()[0]

    def action_conflict(self, db, run_id, ticker, now):
        existing = checked(
            db.execute(
                "SELECT payload,sha256 FROM paper_action_conflicts WHERE run_id=? AND ticker=?",
                (run_id, ticker),
            ).fetchone()
        )
        if existing:
            return existing
        row = db.execute(
            "SELECT payload,sha256 FROM market_price_endpoints WHERE symbol=? AND received_at<=? AND json_array_length(json_extract(payload,'$.actions'))>0 ORDER BY received_at LIMIT 1",
            (ticker, now),
        ).fetchone()
        if not row:
            return None
        endpoint = checked(row)
        value = {
            "run_id": run_id,
            "symbol": ticker,
            "known_at": now,
            "source": endpoint["source"],
            "endpoint_sha256": digest(endpoint),
            "actions": endpoint["actions"][:1],
        }
        raw = encoded(value)
        if self.size(db, run_id) + len(raw.encode()) > MAX_RUN_BYTES:
            self.limit_pause(
                db,
                run_id,
                "Paper evidence limit reached; corporate-action evidence retained at source.",
            )
            return value
        db.execute(
            "INSERT INTO paper_action_conflicts VALUES (?,?,?,?,?)",
            (run_id, ticker, now, raw, digest(value)),
        )
        return value

    def limit_pause(self, db, run_id, reason):
        self.cancel_pending(db, reason, run_id=run_id)
        db.execute("UPDATE paper_run_state SET status='paused' WHERE run_id=?", (run_id,))
        db.execute("UPDATE settings SET value='off' WHERE key='paper_enabled'")
        db.execute("INSERT OR REPLACE INTO state VALUES ('paper_error',?)", (reason,))

    def capture_batch(self, inputs, session, *, now, db, committed_at=None):
        run = self.current(db)
        if not run or run["status"] != "active" or not enabled(self.store, db, "paper", now):
            return None
        config = run["config"]
        scheduled = session.opens - config["decision_before_open_seconds"]
        if (
            not scheduled <= now < scheduled + config["decision_grace_seconds"]
            or (committed_at if committed_at is not None else now) >= session.opens
        ):
            raise ValueError("Paper decision window missed; no backfill.")
        state = db.execute(
            "SELECT state FROM paper_session_state WHERE run_id=? AND session_date=?",
            (run["id"], session.day),
        ).fetchone()
        if state and state[0] != "preparing":
            return None
        portfolio = self.replay(db, run["id"])
        fx = self.fx(db, now)
        fee = money(Decimal(config["fee_eur"]) * Decimal(fx["usd_per_eur"])) if fx else None
        decisions = [classify(value, portfolio["positions"], session, config) for value in inputs]
        batch_id = uuid.uuid4().hex[:16]
        free = portfolio["cash"] - portfolio["reserved"]
        for decision in sorted(decisions, key=lambda d: (-d["score"], d["ticker"])):
            reservation = money(0)
            if decision["action"] in {"buy", "sell"} and fee is None:
                decision["action"] = "hold"
                decision["reasons"].append("fx_unavailable")
            if decision["action"] == "buy":
                price = Decimal(decision["reference_price"])
                needed = max(
                    Decimal(0), Decimal(decision["target_usd"]) - decision["held_quantity"] * price
                )
                reservation = min(free, money(needed + fee))
                if money(price) + fee > reservation:
                    reservation = money(0)
                    decision["action"] = "hold"
                    decision["reasons"].append("insufficient_cash")
                free -= reservation
            decision.update(
                id=uuid.uuid4().hex[:16],
                batch_id=batch_id,
                run_id=run["id"],
                session_date=session.day,
                scheduled_at=scheduled,
                decided_at=now,
                opens=session.opens,
                closes=session.closes,
                reservation_usd=str(reservation),
                fee_estimate=fx,
                config_sha256=digest(config),
            )
        snapshot = {v["ticker"]: v["inputs_sha256"] for v in decisions}
        initial = {k: str(v) for k, v in portfolio.items() if k != "positions"}
        initial["positions"] = {
            k: {"quantity": p["quantity"], "basis": str(p["basis"])}
            for k, p in portfolio["positions"].items()
        }
        batch = {
            "id": batch_id,
            "run_id": run["id"],
            "session_date": session.day,
            "scheduled_at": scheduled,
            "decided_at": now,
            "opens": session.opens,
            "closes": session.closes,
            "initial_portfolio": initial,
            "source_snapshot_sha256": digest(snapshot),
            "decision_inputs": snapshot,
            "config_sha256": digest(config),
        }
        raws = [encoded(v) for v in decisions]
        batch_raw = encoded(batch)
        count = db.execute(
            "SELECT COUNT(*) FROM paper_decisions d JOIN paper_batches b ON b.id=d.batch_id WHERE b.run_id=?",
            (run["id"],),
        ).fetchone()[0]
        projected = (
            self.size(db, run["id"])
            + len(batch_raw.encode())
            + sum(len(r.encode()) for r in raws)
            + sum(d["action"] != "hold" for d in decisions) * MAX_PACKET
        )
        if (
            count + len(decisions) > MAX_DECISIONS
            or projected > MAX_RUN_BYTES
            or len(batch_raw.encode()) > 65536
            or any(len(r.encode()) > MAX_PACKET for r in raws)
        ):
            reason = "Paper evidence limit reached; archival/retention work required."
            self.limit_pause(db, run["id"], reason)
            db.execute(
                "INSERT INTO paper_session_state VALUES (?,?,'cancelled',?) ON CONFLICT(run_id,session_date) DO UPDATE SET state='cancelled',reason=excluded.reason WHERE state='preparing'",
                (run["id"], session.day, reason),
            )
            return None
        db.execute(
            "INSERT INTO paper_batches VALUES (?,?,?,?,?,?,?,?,?)",
            (
                batch_id,
                run["id"],
                session.day,
                scheduled,
                now,
                session.opens,
                session.closes,
                batch_raw,
                digest(batch),
            ),
        )
        for value, raw in zip(decisions, raws):
            db.execute(
                "INSERT INTO paper_decisions VALUES (?,?,?,?,?)",
                (value["id"], batch_id, value["ticker"], raw, digest(value)),
            )
            if value["action"] != "hold":
                db.execute(
                    "INSERT INTO paper_orders VALUES (?,?,'pending',?,?,'')",
                    (value["id"], value["action"], value["reservation_usd"], session.closes + 1800),
                )
                db.execute(
                    "INSERT OR IGNORE INTO paper_price_inputs VALUES (?,?,'open',?,?,NULL)",
                    (value["ticker"], session.day, session.opens + 300, session.closes + 1800),
                )
        db.execute(
            "INSERT INTO paper_session_state VALUES (?,?,'captured','') ON CONFLICT(run_id,session_date) DO UPDATE SET state='captured',reason='' WHERE state='preparing'",
            (run["id"], session.day),
        )
        return batch

    def fill_order(self, decision_id, endpoint, *, db, now):
        existing = checked(
            db.execute(
                "SELECT payload,sha256 FROM paper_fills WHERE decision_id=?", (decision_id,)
            ).fetchone()
        )
        if existing:
            return existing
        decision = checked(
            db.execute(
                "SELECT payload,sha256 FROM paper_decisions WHERE id=?", (decision_id,)
            ).fetchone()
        )
        if not decision:
            raise ValueError("Unknown paper decision.")
        order = db.execute(
            "SELECT side,status,reserved_usd,expires_at FROM paper_orders WHERE decision_id=?",
            (decision_id,),
        ).fetchone()
        if not order or order[1] != "pending":
            return None
        run = self.current(db)
        if (
            not run
            or run["id"] != decision["run_id"]
            or run["status"] != "active"
            or not enabled(self.store, db, "paper", now)
        ):
            self.finish_order(db, decision_id, "disabled_or_paused")
            return None
        if now >= order[3]:
            self.finish_order(db, decision_id, "opening_endpoint_expired", "expired")
            return None
        if now < decision["opens"] + 300:
            return None
        if decision["decided_at"] >= decision["opens"]:
            raise ValueError("Paper instruction was not committed before open.")
        # Re-read immutable parent/endpoint bytes, rather than trusting a caller's mutable cache.
        batch = checked(
            db.execute(
                "SELECT payload,sha256 FROM paper_batches WHERE id=?", (decision["batch_id"],)
            ).fetchone()
        )
        if (
            batch["decided_at"] >= batch["opens"]
            or digest(decision["inputs"]) != decision["inputs_sha256"]
            or decision["config_sha256"] != digest(run["config"])
            or batch["config_sha256"] != decision["config_sha256"]
            or batch["decided_at"] != decision["decided_at"]
        ):
            raise ValueError("Paper decision integrity/knowledge boundary failed.")
        calendar = USCalendar()
        session = calendar.session(date.fromisoformat(decision["session_date"]))
        if (
            not session
            or session.opens != decision["opens"]
            or session.closes != decision["closes"]
            or run["config"]["calendar_version"] != digest(calendar.data)
        ):
            self.finish_order(db, decision_id, "calendar_unavailable_or_changed")
            return None
        if (
            not endpoint
            or endpoint.get("symbol") != decision["ticker"]
            or endpoint.get("session_date") != decision["session_date"]
            or endpoint.get("edge") != "open"
            or endpoint.get("bar_time") != decision["opens"]
            or endpoint.get("source") != SOURCE
            or endpoint.get("method") != METHOD
            or endpoint.get("instrument_type") != "EQUITY"
            or endpoint.get("currency") != "USD"
            or endpoint.get("provider_symbol") != decision["ticker"].replace(".", "-")
            or endpoint.get("timezone") != "America/New_York"
            or not decision["opens"] + 300 <= endpoint.get("received_at", 0) <= now
        ):
            return None
        if endpoint.get("actions") or decision["inputs"].get("corporate_actions"):
            self.action_conflict(db, run["id"], decision["ticker"], now)
            self.finish_order(db, decision_id, "corporate_action_conflict")
            return None
        if self.action_conflict(db, run["id"], decision["ticker"], now):
            self.finish_order(db, decision_id, "corporate_action_conflict")
            return None
        saved = checked(
            db.execute(
                "SELECT payload,sha256 FROM market_price_endpoints WHERE source=? AND symbol=? AND session_date=? AND edge='open' AND method=?",
                (SOURCE, decision["ticker"], decision["session_date"], METHOD),
            ).fetchone()
        )
        if not saved or digest(saved) != digest(endpoint):
            raise ValueError("Fill requires the saved immutable opening endpoint.")
        fx = self.fx(db, decision["opens"])
        if not fx:
            self.finish_order(db, decision_id, "fx_unavailable")
            return None
        other = db.execute(
            "SELECT 1 FROM paper_fills WHERE ticker=? AND session_date=?",
            (decision["ticker"], decision["session_date"]),
        ).fetchone()
        if other:
            self.finish_order(db, decision_id, "ticker_session_already_filled")
            return None
        with localcontext() as ctx:
            ctx.prec = 28
            price = number(endpoint["price"])
            fee = money(Decimal(run["config"]["fee_eur"]) * Decimal(fx["usd_per_eur"]))
            portfolio = self.replay(db, run["id"])
            held = portfolio["positions"].get(decision["ticker"], {}).get("quantity", 0)
            target = int(
                (Decimal(decision["target_usd"]) / price).to_integral_value(rounding=ROUND_FLOOR)
            )
            reservation = money(order[2])
            if decision["action"] == "buy":
                available = min(
                    reservation, portfolio["cash"] - portfolio["reserved"] + reservation
                )
                quantity = min(
                    max(0, target - held),
                    max(
                        0,
                        int(
                            ((available - fee + Decimal("0.005")) / price).to_integral_value(
                                rounding=ROUND_FLOOR
                            )
                        ),
                    ),
                )
                while quantity and money(quantity * price) + fee > available:
                    quantity -= 1
                if quantity and money(quantity * price) + fee > reservation:
                    raise ValueError("Paper debit exceeds its reservation.")
            else:
                quantity = max(0, held - target)
                if quantity and portfolio["cash"] + money(quantity * price) - fee < 0:
                    self.finish_order(db, decision_id, "insufficient_cash")
                    return None
            if quantity <= 0:
                self.finish_order(db, decision_id, "zero_quantity_or_direction_changed")
                return None
            whole(quantity)
            value = {
                "id": uuid.uuid4().hex[:16],
                "decision_id": decision_id,
                "decision_sha256": digest(decision),
                "run_id": run["id"],
                "ticker": decision["ticker"],
                "session_date": decision["session_date"],
                "side": decision["action"],
                "quantity": quantity,
                "price_usd": str(price),
                "fee_eur": run["config"]["fee_eur"],
                "fee_usd": str(fee),
                "executed_at": decision["opens"],
                "recorded_at": now,
                "endpoint": endpoint,
                "endpoint_sha256": digest(endpoint),
                "fx": fx,
                "fx_sha256": digest(fx),
            }
            raw = encoded(value)
            if (
                len(raw.encode()) > MAX_PACKET
                or self.size(db, run["id"]) + len(raw.encode()) > MAX_RUN_BYTES
            ):
                self.limit_pause(
                    db, run["id"], "Paper evidence limit reached; archival/retention work required."
                )
                return None
            db.execute(
                "INSERT INTO paper_fills VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    value["id"],
                    decision_id,
                    run["id"],
                    value["ticker"],
                    value["session_date"],
                    value["side"],
                    quantity,
                    str(price),
                    value["fee_eur"],
                    str(fee),
                    value["executed_at"],
                    now,
                    raw,
                    digest(value),
                ),
            )
            self.finish_order(db, decision_id, "", "filled")
            return value
