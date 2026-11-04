"""Focused filing qualification, cache provenance and notification invariants."""

import copy
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from trabe.api import ApiError
from trabe.insiders import Insiders, normalize
from trabe.signals import SignalStore, WorkStopped
from trabe.store import Store


class InsiderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / "agent.db")
        self.now = datetime(2026, 11, 3, 23, tzinfo=UTC).timestamp()
        self.raw = {
            "filingId": "0001234567-26-000001-0",
            "issuerTicker": "VST",
            "ownerName": "DOE JANE A",
            "ownerTitle": "President and CEO",
            "isDirector": True,
            "isOfficer": True,
            "transactionCode": "P",
            "shares": 1000,
            "pricePerShare": 100,
            "totalValue": 100000,
            "transactionDate": "2026-11-03",
            "filedAt": datetime.fromtimestamp(self.now - 60, UTC).isoformat(),
            "formType": "4",
            "isPairedExercise": False,
            "hasOfferingFootnote": False,
            "hasTaxWithholdingFootnote": False,
            "is10b5_1": False,
            "rawFilingUrl": "https://www.sec.gov/Archives/edgar/data/1234567/000123456726000001/filing-index.htm",
            "url": "https://tracefour.com/insider-trading/VST",
        }
        self.match = {
            "id": "0001234567-26-000001",
            "symbol": "VST",
            "name": "Jane A. Doe",
            "transactionCode": "P",
            "change": 1000,
            "transactionPrice": 100,
            "transactionDate": "2026-11-03",
            "isDerivative": False,
        }
        fixture = self

        class HTTP:
            def __init__(self):
                self.calls = []

            def json(self, url, **kwargs):
                self.calls.append(("tracefour", url))
                return {"data": copy.deepcopy(fixture.trace_rows), "meta": {}}

            def finnhub(self, endpoint, **kwargs):
                self.calls.append(("finnhub", endpoint))
                return {"data": copy.deepcopy(fixture.finnhub_rows)}

        self.trace_rows = [self.raw]
        self.finnhub_rows = [self.match]
        self.http = HTTP()
        self.feature = Insiders(self.store, self.http)
        self.repo = SignalStore(self.store)
        self.store.add("VST", 2)
        with self.store.connection() as db:
            db.execute("INSERT OR REPLACE INTO settings VALUES ('insiders_enabled','on')")
            db.execute("INSERT OR REPLACE INTO settings VALUES ('insider_min_value_usd','10000')")
        self.store.set_state("insiders_enabled_at", str(self.now - 200))

    def test_verified_purchase_has_sources_role_and_distinct_times(self):
        self.feature.refresh(["VST"], now=self.now)
        row = self.repo.rows("insiders", ["VST"])[0]
        value = row["payload"]
        self.assertEqual(value["roles"], ["director", "CEO"])
        self.assertEqual(value["derivative_status"], "non-derivative")
        self.assertEqual(value["value_usd"], "100000")
        self.assertEqual(value["eligibility_errors"], [])
        self.assertEqual(value["published_at"], self.now - 60)
        self.assertEqual(row["observed_at"], self.now)
        self.assertGreaterEqual(row["available_at"], row["observed_at"])
        report = self.feature.report(["VST"], now=self.now)
        self.assertIn("open-market/private", report)
        self.assertIn("Tracefour (CC BY 4.0)", report)
        self.assertIn(self.raw["rawFilingUrl"], report)

    def test_request_receipt_time_accepts_a_genuinely_just_filed_record(self):
        self.http.received_at = self.now + 10
        self.trace_rows = [
            self.raw | {"filedAt": datetime.fromtimestamp(self.now + 5, UTC).isoformat()},
            self.raw
            | {
                "filingId": "0001234567-26-000001-1",
                "filedAt": datetime.fromtimestamp(self.now + 15, UTC).isoformat(),
            },
        ]
        self.feature.refresh(["VST"], now=self.now)
        rows = self.repo.rows("insiders", ["VST"])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["observed_at"], self.now + 10)
        self.assertEqual(rows[0]["payload"]["published_at"], self.now + 5)
        self.assertEqual(rows[0]["payload"]["source_fetched_at"], self.now + 10)
        self.assertGreaterEqual(rows[0]["available_at"], self.now + 10)
        self.assertTrue(
            all(row["succeeded_at"] == self.now + 10 for row in self.repo.status("insiders"))
        )

    def test_filters_derivatives_roles_grants_and_purchase_context(self):
        cases = [
            ({"transactionCode": "A"}, self.match),
            (
                {"isDirector": False, "isOfficer": False, "ownerTitle": "Ten percent owner"},
                self.match,
            ),
            ({"isPairedExercise": True}, self.match),
            ({"hasOfferingFootnote": True}, self.match),
            ({"hasTaxWithholdingFootnote": True}, self.match),
            ({"formType": "4/A"}, self.match),
            ({}, self.match | {"isDerivative": True}),
            ({}, self.match | {"isDerivative": None}),
        ]
        for changes, candidate in cases:
            with self.subTest(changes=changes, derivative=candidate.get("isDerivative")):
                value = normalize(self.raw | changes, [candidate], "VST", self.now)
                self.assertTrue(value["eligibility_errors"])

    def test_crosscheck_needs_all_economic_and_filing_fields(self):
        cases = [
            {"id": "0001234567-26-000002"},
            {"symbol": "GOOGL"},
            {"name": "Another Person"},
            {"transactionDate": "2026-11-02"},
            {"change": 999},
            {"transactionPrice": 99},
            {"transactionCode": "S"},
        ]
        for change in cases:
            with self.subTest(change=change):
                value = normalize(self.raw, [self.match | change], "VST", self.now)
                self.assertEqual(value["derivative_status"], "unknown")

    def test_bad_amount_future_publication_and_unsafe_links(self):
        for changes in (
            {"shares": -1},
            {"pricePerShare": float("nan")},
            {"shares": True},
            {"shares": "1e1000000"},
            {"filedAt": datetime.fromtimestamp(self.now + 60, UTC).isoformat()},
            {"filedAt": "2026-11-03T22:59:00"},
        ):
            self.assertIsNone(normalize(self.raw | changes, [self.match], "VST", self.now))
        value = normalize(
            self.raw
            | {"rawFilingUrl": "https://evil.example/filing", "url": "javascript:alert(1)"},
            [self.match],
            "VST",
            self.now,
        )
        self.assertFalse(value["source_url"])
        self.assertEqual(value["attribution_url"], "https://tracefour.com/filings")
        self.assertTrue(value["eligibility_errors"])

    def test_first_run_seed_and_resume_do_not_send_history(self):
        self.store.set_state("insiders_enabled_at", str(self.now))
        self.feature.refresh(["VST"], now=self.now)
        self.assertEqual(self.feature.alerts(["VST"], now=self.now), [])
        self.store.set_state("insiders_enabled_at", str(self.now - 200))
        self.assertEqual(len(self.feature.alerts(["VST"], now=self.now)), 1)
        self.store.pause_automation(self.now + 30)
        self.assertEqual(self.feature.alerts(["VST"], now=self.now), [])
        self.assertEqual(self.feature.alerts(["VST"], now=self.now + 31), [])
        self.store.pause_automation(0)
        self.store.set_state("insights_resumed_at", str(self.now + 10))
        self.assertEqual(self.feature.alerts(["VST"], now=self.now + 31), [])

    def test_claim_survives_restart_minimum_edits_and_repeated_polls(self):
        self.feature.refresh(["VST"], now=self.now)
        key, message = self.feature.alerts(["VST"], now=self.now)[0]
        self.assertTrue(
            self.repo.notify_once(
                123, "insiders", key, message, now=self.now, setting="insiders_enabled"
            )
        )
        self.feature.refresh(["VST"], now=self.now + 1, force=True)
        with self.store.connection() as db:
            db.execute("UPDATE settings SET value='1' WHERE key='insider_min_value_usd'")
        restarted = Insiders(Store(self.store.path), self.http)
        self.assertEqual(restarted.alerts(["VST"], now=self.now + 2), [])
        self.assertEqual(len(self.repo.rows("insiders", ["VST"])), 1)
        with self.store.connection() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0], 1)

    def test_distinct_ordinals_are_retained_and_amendments_cannot_alert(self):
        second = self.raw | {
            "filingId": "0001234567-26-000001-1",
            "shares": 2000,
            "totalValue": 200000,
        }
        amendment = self.raw | {"filingId": "0001234567-26-000002-0", "formType": "4/A"}
        self.trace_rows.extend([second, amendment])
        self.finnhub_rows.append(self.match | {"change": 2000})
        self.feature.refresh(["VST"], now=self.now)
        self.assertEqual(len(self.repo.rows("insiders", ["VST"])), 3)
        self.assertEqual(len(self.feature.alerts(["VST"], now=self.now)), 1)
        original = self.repo.get("insiders", "VST:" + self.raw["filingId"])["payload"]
        self.assertIn("superseded by amendment", " ".join(original["eligibility_errors"]))

    def test_minimum_value_is_evaluated_using_current_setting(self):
        self.feature.refresh(["VST"], now=self.now)
        with self.store.connection() as db:
            db.execute("UPDATE settings SET value='150000' WHERE key='insider_min_value_usd'")
        self.assertEqual(self.feature.alerts(["VST"], now=self.now), [])
        self.assertIn(
            "below configured value threshold", self.feature.report(["VST"], now=self.now)
        )

    def test_unavailable_verification_stays_visible_and_unqualified(self):
        def denied(*args, **kwargs):
            raise ApiError("secret provider body", 403)

        self.http.finnhub = denied
        self.feature.refresh(["VST"], now=self.now)
        report = self.feature.report(["VST"], now=self.now)
        self.assertIn("non-derivative eligibility not verified", report)
        self.assertNotIn("secret provider body", report)
        self.assertEqual(self.feature.alerts(["VST"], now=self.now), [])

    def test_cache_and_source_outage_preserve_prior_records(self):
        self.feature.refresh(["VST"], now=self.now)
        self.feature.refresh(["VST"], now=self.now + 1)
        self.assertEqual(len(self.http.calls), 2)

        def offline(*args, **kwargs):
            raise OSError("private network details")

        self.http.json = offline
        self.feature.refresh(["VST"], now=self.now + 7 * 3600)
        self.assertEqual(len(self.repo.rows("insiders", ["VST"])), 1)
        self.assertIn(
            "Tracefour unavailable", self.feature.report(["VST"], now=self.now + 7 * 3600)
        )
        self.assertNotIn(
            "private network details", self.feature.report(["VST"], now=self.now + 7 * 3600)
        )

    def test_unsupported_symbol_and_cancellation_are_explicit(self):
        self.feature.refresh(["BRK.B"], now=self.now)
        self.assertEqual(self.http.calls, [])
        self.assertIn("unsupported", self.feature.report(["BRK.B"], now=self.now))

        def stopped(*args, **kwargs):
            raise WorkStopped("paused")

        self.http.json = stopped
        with self.assertRaises(WorkStopped):
            self.feature.refresh(["VST"], now=self.now)
        self.assertTrue(
            all(not row["error"] for row in self.repo.status("insiders") if row["symbol"] == "VST")
        )

    def test_briefing_is_bounded_and_has_attribution(self):
        self.feature.refresh(["VST"], now=self.now)
        lines = self.feature.briefing(["VST"], now=self.now, limit=1)
        self.assertEqual(len(lines), 1)
        self.assertIn("Tracefour", lines[0])
        self.assertIn(self.raw["rawFilingUrl"], lines[0])


if __name__ == "__main__":
    unittest.main()
