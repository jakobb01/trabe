import io
import tempfile
import unittest
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from unittest.mock import Mock

from trabe.fx import FX
from trabe.ledger import Ledger
from trabe.outbox import Outbox
from trabe.portfolio import Portfolio
from trabe.store import Store


class PortfolioTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / "portfolio.db")
        self.ledger = Ledger(self.store)
        self.now = datetime(2026, 11, 3, 20, tzinfo=UTC).timestamp()

    def quote_and_rate(self):
        with self.store.connection() as db:
            db.execute(
                "INSERT INTO fx_cache VALUES (?,?,?,?,?)",
                ("2026-11-03", str(Decimal(10) / Decimal(9)), "2026-11-03", "fixture", self.now),
            )
        self.store.save_live(
            {
                "ticker": "GOOGL",
                "provider": "finnhub",
                "currency": "USD",
                "price": 120,
                "source_time": self.now,
                "received_at": self.now,
            }
        )

    def test_required_eur_example_and_missing_price(self):
        self.ledger.record("GOOGL", "buy", "5", "100", "EUR", "2026-11-02")
        report = Portfolio(self.store).report(now=self.now)
        self.assertIn("Market value: unavailable", report)
        self.quote_and_rate()
        report = Portfolio(self.store).report(now=self.now)
        for text in (
            "Converted price: 108.00 EUR",
            "Market value: 540.00 EUR",
            "Unrealized P/L: +40.00 EUR (+8.00%)",
        ):
            self.assertIn(text, report)

    def test_historical_fx_is_saved_and_currency_change_preserves_basis(self):
        self.ledger.record("GOOGL", "buy", "5", "100", "EUR", "2026-10-31")

        def opener(request, timeout):
            self.assertIn("/2026-", request.full_url)
            return io.BytesIO(b'{"base":"EUR","date":"2026-10-30","rates":{"USD":1.1}}')

        FX(self.store, opener).refresh(self.now)
        self.assertEqual(self.ledger.positions("EUR")["GOOGL"][1], Decimal("500"))
        self.assertEqual(self.ledger.positions("USD")["GOOGL"][1], Decimal("550"))
        history = self.ledger.history("GOOGL")[0]
        self.assertEqual(history[-3:-1], ("2026-10-30", "Frankfurter/ECB"))

    def test_new_capital_is_excluded_from_progress(self):
        self.ledger.record("GOOGL", "buy", "5", "100", "EUR", "2026-11-02")
        self.quote_and_rate()
        self.store.set_setting("portfolio_summary_time", "00:00")
        portfolio = Portfolio(self.store)
        outbox = Outbox(self.store)
        portfolio.schedule(123, outbox, self.now)
        self.ledger.record("GOOGL", "buy", "1", "100", "EUR", "2026-11-03")
        portfolio.schedule(123, outbox, self.now + 86400)
        with self.store.connection() as db:
            messages = "\n".join(
                row[0] for row in db.execute("SELECT text FROM outbox ORDER BY id")
            )
        self.assertIn("No unchanged positions with comparable prices", messages)
        self.assertIn("Excluded due to trades", messages)

    def test_provisional_rate_is_not_frozen_after_outage(self):
        self.ledger.record("GOOGL", "buy", "1", "100", "USD", "2026-11-02")
        # A cached Monday request made before Monday's reference was published.
        provisional = datetime(2026, 11, 2, 10, tzinfo=UTC).timestamp()
        with self.store.connection() as db:
            db.execute(
                "INSERT INTO fx_cache VALUES (?,?,?,?,?)",
                ("2026-11-02", "1.1", "2026-10-30", "fixture", provisional),
            )

        def offline(request, timeout):
            raise OSError("offline")

        FX(self.store, offline).refresh(self.now)
        self.assertIsNone(self.ledger.positions("EUR")["GOOGL"][1])

    def test_missing_historical_fx_never_becomes_one_to_one(self):
        self.ledger.record("GOOGL", "buy", "1", "100", "USD", "2026-11-02")

        def offline(request, timeout):
            raise OSError("offline")

        FX(self.store, offline).refresh(self.now)
        self.assertIsNone(self.ledger.positions("EUR")["GOOGL"][1])
        self.assertEqual(self.ledger.positions("USD")["GOOGL"][1], Decimal("100"))

    def test_failed_old_dates_do_not_starve_later_backfills(self):
        for day in ("2026-10-26", "2026-10-27", "2026-10-28", "2026-10-29"):
            self.ledger.record("GOOGL", "buy", "1", "100", "USD", day)
        called = []

        def opener(request, timeout):
            called.append(request.full_url)
            raise OSError("offline")

        worker = FX(self.store, opener)
        worker.refresh(self.now)
        worker.refresh(self.now + 60)
        self.assertTrue(any("/2026-10-29?" in url for url in called))

    def test_pause_during_fx_batch_stops_the_next_request(self):
        self.ledger.record("GOOGL", "buy", "1", "100", "USD", "2026-11-02")
        called = []

        def opener(request, timeout):
            called.append(request.full_url)
            self.store.pause_automation(datetime.now(UTC).timestamp() + 60)
            return io.BytesIO(b'{"base":"EUR","date":"2026-11-03","rates":{"USD":1.1}}')

        FX(self.store, opener).refresh(self.now)
        self.assertEqual(len(called), 1)

    def test_usd_trade_uses_inverse_historical_rate(self):
        self.ledger.record("GOOGL", "buy", "2", "110", "USD", "2026-10-31", fee="1")

        def opener(request, timeout):
            return io.BytesIO(b'{"base":"EUR","date":"2026-10-30","rates":{"USD":1.1}}')

        FX(self.store, opener).refresh(self.now)
        self.assertAlmostEqual(
            self.ledger.positions("EUR")["GOOGL"][1], Decimal("221") / Decimal("1.1")
        )
        self.assertEqual(self.ledger.positions("USD")["GOOGL"][1], Decimal("221"))
        with self.store.connection() as db:
            self.assertEqual(
                db.execute("SELECT fx_from,fx_to FROM trades").fetchone(), ("USD", "EUR")
            )

    def test_pause_suppresses_background_fx(self):
        from trabe.config import Config
        from trabe.workers import Services

        config = Config("123:" + "a" * 32, 123, "key", self.store.path)
        services = Services(self.store, config, object(), Outbox(self.store), object())
        services.stop = Mock()
        services.stop.is_set.side_effect = [False, True]
        services.fx = Mock()
        services.portfolio = Mock()
        self.store.pause_automation(datetime.now(UTC).timestamp() + 60)
        services.portfolio_work()
        services.fx.refresh.assert_not_called()
        services.portfolio.schedule.assert_called_once()

    def test_closed_position_has_zero_value_without_a_quote(self):
        self.ledger.record("GOOGL", "buy", "1", "100", "EUR", "2026-11-02")
        self.ledger.record("GOOGL", "sell", "1", "120", "EUR", "2026-11-03")
        report = Portfolio(self.store).report("GOOGL", self.now)
        self.assertIn("Market value: 0.00 EUR", report)
        self.assertIn("Realized P/L: +20.00 EUR", report)

    def test_held_symbol_coverage_and_subscription_limit(self):
        from trabe.config import Config
        from trabe.monitor import Monitor

        for i in range(49):
            self.store.add(f"S{i}", 2)
        self.ledger.record("GOOGL", "buy", "1", "100", "EUR", "2026-11-02")
        self.assertEqual(len(self.ledger.coverage()), 50)
        with self.assertRaises(ValueError):
            self.ledger.record("MSFT", "buy", "1", "100", "EUR", "2026-11-02")
        config = Config("123:" + "a" * 32, 123, "key", self.store.path)
        monitor = Monitor(self.store, config, None)
        _, _, stocks = monitor.controls()
        self.assertIn("GOOGL", stocks)
        self.assertFalse(monitor.engine.rules["GOOGL"][0])
        self.ledger.record("GOOGL", "sell", "1", "100", "EUR", "2026-11-03")
        self.assertNotIn("GOOGL", self.ledger.coverage())

    def test_summary_deduplicates_and_pause_keeps_manual_report(self):
        self.ledger.record("GOOGL", "buy", "5", "100", "EUR", "2026-11-02")
        self.quote_and_rate()
        self.store.set_setting("portfolio_summary_time", "00:00")
        portfolio = Portfolio(self.store)
        outbox = Outbox(self.store)
        self.store.pause_automation(self.now + 100)
        portfolio.schedule(123, outbox, self.now)
        self.assertEqual(outbox.pending(), 0)
        self.assertIn("GOOGL", portfolio.report(now=self.now))
        self.store.pause_automation(0)
        portfolio.schedule(123, outbox, self.now)
        first = outbox.pending()
        self.assertGreater(first, 0)
        portfolio.schedule(123, outbox, self.now)
        self.assertEqual(outbox.pending(), first)


class BotPortfolioTests(unittest.TestCase):
    def setUp(self):
        from trabe.api import MarketData
        from trabe.bot import Bot
        from trabe.config import Config

        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / "bot.db")
        self.messages = []

        class Telegram:
            def send(inner, chat_id, text, **kwargs):
                self.messages.append(text)

        self.bot = Bot(
            Config("123:" + "a" * 32, 123, "key", self.store.path),
            self.store,
            Telegram(),
            MarketData("key", self.store),
        )

    def update(self, text, update_id, owner=123):
        return {
            "update_id": update_id,
            "message": {
                "text": text,
                "from": {"id": owner},
                "chat": {"id": owner, "type": "private"},
            },
        }

    def test_owner_replay_and_trade_correction(self):
        self.bot.process(self.update("/buy GOOGL 5 100 EUR 2026-01-02 fee=2", 1, owner=999))
        self.assertEqual(self.bot.ledger.history("GOOGL"), [])
        self.bot.process(self.update("/buy GOOGL 5 100 EUR 2026-01-02 fee=2", 2))
        self.bot.process(self.update("/buy GOOGL 5 100 EUR 2026-01-02 fee=2", 2))
        self.bot.process(self.update("/sell GOOGL 2 125 EUR 2026-01-03", 3))
        self.assertEqual(
            self.bot.ledger.positions("EUR")["GOOGL"],
            (Decimal("3"), Decimal("301.2"), Decimal("49.2")),
        )
        self.bot.process(self.update("/trade_void 1", 4))
        self.assertIn("exceeds shares", self.messages[-1])
        self.bot.process(self.update("/trade_void 2", 5))
        self.bot.process(self.update("/trade_void 1", 6))
        self.assertEqual(self.bot.ledger.held(), set())
        self.assertEqual(len(self.bot.ledger.history("GOOGL")), 2)

    def test_adjustments_preserve_history_and_quantity(self):
        self.bot.process(self.update("/buy GOOGL 5 100 EUR 2026-01-02", 1))
        self.bot.process(self.update("/position_adjust GOOGL 10 2026-01-03", 2))
        self.bot.process(self.update("/basis_adjust GOOGL 600 EUR 2026-01-04", 3))
        self.assertEqual(
            self.bot.ledger.positions("EUR")["GOOGL"][:2], (Decimal("10"), Decimal("600"))
        )
        self.assertEqual(len(self.bot.ledger.history("GOOGL")), 3)


if __name__ == "__main__":
    unittest.main()
