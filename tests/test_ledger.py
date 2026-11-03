import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from trabe.ledger import Ledger
from trabe.store import Store


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ledger = Ledger(Store(Path(self.tmp.name) / "test.db"))

    def test_weighted_basis_partial_sale_and_deduplication(self):
        first = self.ledger.record("GOOGL", "buy", "5", "100", "EUR", "2026-01-02", update_id=1)
        self.assertEqual(
            first, self.ledger.record("GOOGL", "buy", "5", "100", "EUR", "2026-01-02", update_id=1)
        )
        self.ledger.record("GOOGL", "buy", "5", "120", "EUR", "2026-01-03")
        self.ledger.record("GOOGL", "sell", "6", "150", "EUR", "2026-01-04")
        self.assertEqual(
            self.ledger.positions("EUR")["GOOGL"], (Decimal("4"), Decimal("440"), Decimal("240"))
        )
        with self.assertRaises(ValueError):
            self.ledger.void(first)
        with self.assertRaises(ValueError):
            self.ledger.record("GOOGL", "sell", "5", "1", "EUR", "2026-01-05")

    def test_missing_fx_is_unavailable(self):
        self.ledger.record("GOOGL", "buy", "1", "100", "USD", "2026-01-02")
        self.assertIsNone(self.ledger.positions("EUR")["GOOGL"][1])
        self.assertEqual(self.ledger.positions("USD")["GOOGL"][1], Decimal("100"))


if __name__ == "__main__":
    unittest.main()
