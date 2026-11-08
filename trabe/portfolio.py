"""Cached portfolio valuation and durable, opt-in daily summaries."""

import json
import time
from datetime import UTC, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from trabe.calendar import USCalendar
from trabe.fx import FX
from trabe.ledger import Ledger


class Portfolio:
    def __init__(self, store, monitor=None):
        self.store, self.monitor = store, monitor
        self.ledger = Ledger(store)
        self.fx = FX(store)

    def calculate(self, ticker=None, now=None, db=None):
        now = time.time() if now is None else now
        settings = self.store.settings(db)
        currency = settings["default_currency"]
        positions = self.ledger.positions(currency, db)
        fx = self.fx.current(now, db)
        rows = []
        for name, (quantity, basis, realized) in sorted(positions.items()):
            if ticker and name != ticker:
                continue
            if quantity == 0 and not ticker:
                continue
            quote = self.monitor.latest(name) if self.monitor and db is None else None
            quote = quote or self.store.cached_live("finnhub", name, db)
            if (
                db is not None
                and quote
                and (
                    quote.get("received_at", now + 1) > now
                    or quote.get("source_time", now + 1) > now
                )
            ):
                quote = None
            converted = value = profit = None
            if quantity == 0:
                value = Decimal(0)
                profit = Decimal(0) if basis == 0 else None
            if quote and quote.get("currency") == "USD" and quote.get("price"):
                price = Decimal(str(quote["price"]))
                if currency == "USD":
                    converted = price
                elif fx:
                    converted = price / fx[0]
                if converted is not None:
                    value = quantity * converted
                    if basis is not None:
                        profit = value - basis
            rows.append(
                dict(
                    ticker=name,
                    quantity=quantity,
                    basis=basis,
                    realized=realized,
                    quote=quote,
                    converted=converted,
                    value=value,
                    profit=profit,
                )
            )
        return currency, fx, rows

    def report(self, ticker=None, now=None, calculated=None):
        now = time.time() if now is None else now
        currency, fx, rows = calculated if calculated is not None else self.calculate(ticker, now)
        if not rows:
            return "No recorded position." if ticker else "No open positions."
        result = [f"{'Position' if ticker else 'Portfolio'} • {currency}"]
        total_value = total_basis = total_profit = Decimal(0)
        complete_value = complete_basis = complete_profit = True
        for row in rows:
            quantity, basis, quote, value, profit = (
                row[k] for k in ("quantity", "basis", "quote", "value", "profit")
            )
            result.append(f"\n{row['ticker']} • {quantity} shares")
            if basis is None:
                result.append("Entry cost and average: unavailable (historical FX pending)")
                complete_basis = False
            else:
                total_basis += basis
                result.append(
                    f"Cost basis: {basis:.2f} {currency}"
                    + (f" • average {basis / quantity:.2f} {currency}" if quantity else "")
                )
            if row["realized"] is None:
                result.append("Realized P/L: unavailable (historical FX pending)")
            else:
                result.append(f"Realized P/L: {row['realized']:+.2f} {currency}")
            if quote is None:
                result.append("Market price unavailable (no cached Finnhub quote).")
            else:
                source_time = quote.get("source_time", 0)
                source = (
                    datetime.fromtimestamp(source_time, UTC).isoformat()
                    if source_time
                    else "unknown"
                )
                age = max(0, now - source_time) if source_time else float("inf")
                try:
                    session = USCalendar().at(now)
                    market = "open" if session and session.contains(now) else "closed"
                except ValueError:
                    market = "session unknown"
                freshness = (
                    "stale"
                    if age > int(self.store.settings()["max_source_age_seconds"])
                    else "fresh"
                )
                result.append(
                    f"Finnhub: {quote.get('price')} USD • stock time {source} • {market}, {freshness}"
                )
                baseline = self.store.baseline(row["ticker"], quote.get("trading_day", ""))
                if not quote.get("baseline_valid", True) or (baseline and not baseline["accepted"]):
                    result.append(
                        "Price baseline flagged for review; check possible corporate action or feed mismatch."
                    )
                if row["converted"] is not None:
                    result.append(f"Converted price: {row['converted']:.2f} {currency}")
                elif currency == "EUR":
                    result.append("Converted price unavailable (current FX missing or stale).")
            if value is None:
                complete_value = False
                result.append("Market value: unavailable")
            else:
                total_value += value
                result.append(f"Market value: {value:.2f} {currency}")
            if profit is None:
                complete_profit = False
                result.append("Unrealized P/L: unavailable")
            else:
                total_profit += profit
                percentage = f" ({profit / basis * 100:+.2f}%)" if basis else ""
                result.append(f"Unrealized P/L: {profit:+.2f} {currency}{percentage}")
        if not ticker:
            result.extend(
                [
                    "",
                    f"Cost basis{' (partial)' if not complete_basis else ''}: {total_basis:.2f} {currency}",
                    f"Market value{' (partial)' if not complete_value else ''}: {total_value:.2f} {currency}",
                    f"Unrealized P/L{' (partial)' if not complete_profit else ''}: {total_profit:+.2f} {currency}",
                ]
            )
            if not (complete_basis and complete_value and complete_profit):
                result.append("Partial totals exclude unavailable amounts.")
        if fx:
            result.append(
                f"Daily reference FX: 1 EUR = {fx[0]} USD • published {fx[1]} • {fx[2]} (not broker execution rate)."
            )
        else:
            result.append("Daily reference FX unavailable or older than seven calendar days.")
        result.append(
            "Returns in the reporting currency include stock and FX movements; fees are included."
        )
        return "\n".join(result)

    def schedule(self, owner_id, outbox, now=None):
        now = time.time() if now is None else now
        settings = self.store.settings()
        scheduled = settings["portfolio_summary_time"]
        if scheduled == "off" or self.store.automation_paused(now):
            return
        local = datetime.fromtimestamp(now, ZoneInfo(settings["timezone"]))
        day = local.date().isoformat()
        if local.strftime("%H:%M") < scheduled:
            return
        with self.store.connection() as db:
            original_history = db.execute(
                "SELECT id,ticker,voided_at FROM trades ORDER BY id"
            ).fetchall()
        calculated = self.calculate(now=now)
        currency, _, rows = calculated
        if not rows:
            return
        complete = all(row["value"] is not None and row["basis"] is not None for row in rows)
        values = {r["ticker"]: str(r["value"]) for r in rows if r["value"] is not None}
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM portfolio_snapshots WHERE day=?", (day,)).fetchone():
                return
            current_settings = dict(db.execute("SELECT key,value FROM settings"))
            paused = db.execute("SELECT value FROM state WHERE key='pause_until'").fetchone()
            current_history = db.execute(
                "SELECT id,ticker,voided_at FROM trades ORDER BY id"
            ).fetchall()
            if (
                current_history != original_history
                or current_settings["default_currency"] != currency
                or current_settings["portfolio_summary_time"] != scheduled
                or current_settings["timezone"] != settings["timezone"]
                or (paused and float(paused[0]) > now)
            ):
                return  # Retry with a coherent view on the next worker pass.
            histories = {}
            for trade_id, symbol, voided in current_history:
                histories.setdefault(symbol, []).append([trade_id, voided])
            previous = db.execute(
                "SELECT currency,payload FROM portfolio_snapshots WHERE day<? ORDER BY day DESC LIMIT 1",
                (day,),
            ).fetchone()
            payload = {"complete": complete, "values": values, "histories": histories}
            message = "Daily progress • " + day + "\n" + self.report(now=now, calculated=calculated)
            if previous:
                old_currency, old_json = previous
                old = json.loads(old_json)
                old_values, old_histories = old.get("values", {}), old.get("histories", {})
                if old_currency == currency:
                    comparable = {
                        symbol
                        for symbol in values.keys() & old_values.keys()
                        if histories.get(symbol) == old_histories.get(symbol)
                    }
                    excluded = ({r["ticker"] for r in rows} | set(old_values)) - comparable
                    if comparable:
                        change = sum(
                            (Decimal(values[k]) - Decimal(old_values[k]) for k in comparable),
                            Decimal(0),
                        )
                        message += f"\nChange since previous snapshot for unchanged positions: {change:+.2f} {currency}."
                    else:
                        message += "\nNo unchanged positions with comparable prices for progress."
                    if excluded:
                        message += (
                            " Excluded due to trades, corrections, missing prices, or closed positions: "
                            + ", ".join(sorted(excluded))
                            + "."
                        )
                else:
                    message += (
                        "\nProgress comparison unavailable because reporting currency changed."
                    )
            else:
                message += "\nFirst snapshot; no previous comparison."
            contributors = sorted(
                (r for r in rows if r["profit"] is not None),
                key=lambda r: abs(r["profit"]),
                reverse=True,
            )[:3]
            if contributors:
                message += "\nLargest unrealized contributors: " + ", ".join(
                    f"{r['ticker']} {r['profit']:+.2f} {currency}" for r in contributors
                )
            outbox.send(owner_id, message, kind="summary", db=db)
            db.execute(
                "INSERT INTO portfolio_snapshots VALUES (?,?,?)",
                (day, currency, json.dumps(payload)),
            )
