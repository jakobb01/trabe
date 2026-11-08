"""Durable transaction ledger and weighted-average position arithmetic."""

from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation


def number(value, *, positive=False):
    try:
        result = Decimal(str(value))
    except InvalidOperation:
        raise ValueError("Quantity, price and fee must be decimal numbers.") from None
    if not result.is_finite() or result < 0 or (positive and not result):
        raise ValueError("Quantity and price must be positive; fee must be nonnegative.")
    return result


def replay(rows, currency):
    """Recalculate chronologically; missing FX is propagated, never guessed."""
    quantity = Decimal(0)
    basis = Decimal(0)
    realized = Decimal(0)
    for row in rows:
        _, side, qty, price, original, _, fee, rate, _, _ = row
        qty, price, fee = map(Decimal, (qty, price, fee))
        if side == "basis":
            if quantity <= 0:
                raise ValueError("Cannot adjust a closed position.")
            basis = price if currency == original else (price * Decimal(rate) if rate else None)
        elif side == "adjust":
            if quantity <= 0:
                raise ValueError("Cannot adjust a closed position.")
            quantity = qty
        elif side == "buy":
            quantity += qty
            amount = qty * price + fee
            converted = (
                amount if currency == original else (amount * Decimal(rate) if rate else None)
            )
            basis = None if basis is None or converted is None else basis + converted
        else:
            if qty > quantity:
                raise ValueError("Sale exceeds shares available on its execution date.")
            allocated = None if basis is None else basis * qty / quantity
            amount = qty * price - fee
            converted = (
                amount if currency == original else (amount * Decimal(rate) if rate else None)
            )
            realized = (
                None
                if realized is None or allocated is None or converted is None
                else realized + converted - allocated
            )
            basis = None if basis is None else basis - allocated
            quantity -= qty
            if quantity == 0:
                basis = Decimal(0)
    return quantity, basis, realized


class Ledger:
    def __init__(self, store):
        self.store = store

    @staticmethod
    def _rows(db, ticker, exclude=None):
        return db.execute(
            """SELECT id,side,quantity,price,currency,trade_date,fee,
            fx_rate,fx_date,fx_provider FROM trades WHERE ticker=? AND voided_at IS NULL
            AND (? IS NULL OR id != ?) ORDER BY trade_date,id""",
            (ticker, exclude, exclude),
        ).fetchall()

    def record(self, ticker, side, quantity, price, currency, trade_date, fee="0", update_id=None):
        quantity, price, fee = (
            number(quantity, positive=True),
            number(price, positive=side != "basis"),
            number(fee),
        )
        if side not in ("buy", "sell", "adjust", "basis") or currency not in ("EUR", "USD"):
            raise ValueError("Invalid transaction type or currency.")
        try:
            day = date.fromisoformat(trade_date)
        except ValueError:
            raise ValueError("Use a date in YYYY-MM-DD format.") from None
        if day.isoformat() != trade_date:
            raise ValueError("Use a date in YYYY-MM-DD format.")
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if update_id is not None:
                existing = db.execute(
                    "SELECT id FROM trades WHERE update_id=?", (update_id,)
                ).fetchone()
                if existing:
                    return existing[0]
            watched = {row[0] for row in db.execute("SELECT ticker FROM watchlist")}
            previous_count = len(watched | self._held(db))
            cursor = db.execute(
                """INSERT INTO trades(ticker,side,quantity,price,currency,trade_date,fee,created_at,update_id)
                VALUES (?,?,?,?,?,?,?,?,?)""",
                (
                    ticker,
                    side,
                    str(quantity),
                    str(price),
                    currency,
                    trade_date,
                    str(fee),
                    datetime.now(UTC).isoformat(),
                    update_id,
                ),
            )
            replay(self._rows(db, ticker), currency)
            held = self._held(db)
            if len(watched | held) > 50 and len(watched | held) > previous_count:
                raise ValueError(
                    "Free monitoring supports 50 held and watched symbols together. Remove a watchlist symbol before adding this position."
                )
            return cursor.lastrowid

    def void(self, trade_id):
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT ticker,voided_at FROM trades WHERE id=?", (trade_id,)
            ).fetchone()
            if not row or row[1]:
                raise ValueError("Trade not found or already voided.")
            replay(self._rows(db, row[0], trade_id), "EUR")
            watched = {r[0] for r in db.execute("SELECT ticker FROM watchlist")}
            previous_count = len(watched | self._held(db))
            db.execute(
                "UPDATE trades SET voided_at=? WHERE id=?",
                (datetime.now(UTC).isoformat(), trade_id),
            )
            current_count = len(watched | self._held(db))
            if current_count > 50 and current_count > previous_count:
                raise ValueError("Voiding this trade would exceed the 50-symbol monitoring limit.")
            return row[0]

    def rows(self, ticker):
        with self.store.connection() as db:
            return self._rows(db, ticker)

    def positions(self, currency, db=None):
        if db is None:
            with self.store.connection() as connection:
                return self.positions(currency, connection)
        symbols = [
            r[0] for r in db.execute("SELECT DISTINCT ticker FROM trades WHERE voided_at IS NULL")
        ]
        return {ticker: replay(self._rows(db, ticker), currency) for ticker in symbols}

    def history(self, ticker):
        with self.store.connection() as db:
            return db.execute(
                """SELECT id,side,quantity,price,currency,trade_date,fee,voided_at,fx_rate,fx_date,fx_provider,fx_retrieved
                FROM trades WHERE ticker=? ORDER BY trade_date,id""",
                (ticker,),
            ).fetchall()

    @classmethod
    def _held(cls, db):
        symbols = [
            row[0]
            for row in db.execute("SELECT DISTINCT ticker FROM trades WHERE voided_at IS NULL")
        ]
        return {ticker for ticker in symbols if replay(cls._rows(db, ticker), "EUR")[0] > 0}

    def held(self):
        with self.store.connection() as db:
            return self._held(db)

    def coverage(self):
        with self.store.connection() as db:
            return self._held(db) | {row[0] for row in db.execute("SELECT ticker FROM watchlist")}
