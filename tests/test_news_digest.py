"""Across-universe prominence, Unicode budgets and cached financial-input isolation."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from trabe.config import Config
from trabe.insights import Insights
from trabe.news import News
from trabe.news_digest import NewsDigest, card, units
from trabe.news_event_store import EventStore
from trabe.news_events import analyze
from trabe.outbox import Outbox
from trabe.store import Store


class DigestTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = Store(Path(directory.name) / "digest.db")
        self.now = 1794657600.0
        self.repo = EventStore(self.store)
        self.config = Config("100:" + "a" * 32, 123, "", self.store.path)
        self.insights = Insights(self.config, self.store, Outbox(self.store))

    def item(self, headline, identity, *, snippet="", published=None):
        return {
            "headline": headline,
            "snippet": snippet,
            "provider": "fixture",
            "item_id": identity,
            "url": "https://example.com/" + identity,
            "published_at": published or self.now - 60,
            "input_quality": "snippet" if snippet else "headline_only",
        }

    def test_compact_briefing_keeps_material_company_news_before_routine_detail(self):
        self.repo.ingest([self.item("NVIDIA announces CEO transition", "leader")], "NVDA", self.now)
        for symbol in ("AMD", "AMZN", "GOOGL", "NVDA", "VST"):
            self.store.add(symbol, 2)
        with patch("trabe.signals.SignalHTTP._read", side_effect=AssertionError("cached network")):
            rendered = self.insights.render_briefing(
                ["AMD", "AMZN", "GOOGL", "NVDA", "VST"], self.now
            )
        self.assertIn("NVIDIA announces CEO transition", rendered)
        self.assertNotIn("https://", rendered)
        self.assertNotIn("Coverage is partial", rendered)
        self.assertIn("Price impact unknown", rendered)
        self.assertLessEqual(units(rendered), 3300)
        self.assertLess(rendered.index("NVIDIA announces"), rendered.index("Earnings"))

    def test_unicode_omission_notice_and_source_qualifications_are_within_budget(self):
        for j, symbol in enumerate(("NVDA", "VST", "AMZN")):
            title = symbol + " raises full-year guidance"
            snippet = symbol + " says " + "🧪" * 190 + "; the scope remains unverified."
            self.repo.ingest([self.item(title, str(j), snippet=snippet)], symbol, self.now + j)
        with patch.object(self.insights.market_news(), "briefing", return_value=[]):
            rendered = self.insights.render_briefing(["NVDA", "VST", "AMZN"], self.now + 3)
        self.assertLessEqual(units(rendered), 3300)
        self.assertNotIn("https://", rendered)
        visible = sum(
            symbol + " raises full-year guidance" in rendered for symbol in ("NVDA", "VST", "AMZN")
        )
        self.assertGreater(visible, 0)
        self.assertGreaterEqual(rendered.count("Rule interpretation; impact unvalidated."), visible)

    def test_seven_day_context_keeps_original_date_and_no_current_claim(self):
        self.repo.ingest(
            [
                self.item(
                    "NVIDIA announces CEO transition", "earlier", published=self.now - 5 * 86400
                )
            ],
            "NVDA",
            self.now,
        )
        report = NewsDigest(self.store).report(["NVDA"], self.now)
        self.assertIn("No current material", report)
        self.assertIn("Earlier context", report)
        self.assertIn("120h ago", report)
        self.assertNotIn("Nov", report)
        self.assertNotIn("UTC", report)
        self.assertEqual(NewsDigest(self.store).material(["NVDA"], self.now), [])

    def test_attribution_ambiguity_short_tickers_and_negative_qualifiers_are_conservative(self):
        item = self.item("NVIDIA says AMD appoints a new CEO", "multi")
        result = analyze(item, "AMD")
        self.assertIsNone(result["actor"])
        self.assertFalse(result["push_eligible"])
        self.assertEqual(result["materiality"], "low")
        self.assertIsNone(analyze(self.item("A report on markets", "word"), "ON")["issuer"])
        negated = self.item("NVIDIA denies CEO departure rumors", "denial")
        self.repo.ingest([negated], "NVDA", self.now)
        report = card(self.repo.events(["NVDA"], self.now)[0])
        self.assertIn(negated["headline"], report)
        self.assertIn("Unconfirmed/proposed", report)
        self.assertIn("unknown", report)

    def test_mirroring_failure_preserves_legacy_success_and_records_processing_gap(self):
        from test_news import FakeHTTP

        http = FakeHTTP()
        http.data["NVDA"] = [
            {
                "headline": "NVIDIA raises guidance",
                "summary": "NVIDIA raises guidance.",
                "url": "https://example.com/input",
                "datetime": self.now - 60,
                "related": "NVDA",
                "id": "one",
            }
        ]
        with patch.object(EventStore, "ingest", side_effect=RuntimeError("presentation fault")):
            result = News(self.store, http).refresh(["NVDA"], now=self.now)
        self.assertEqual(result["refreshed"], 1)
        self.assertIn("legacy collection succeeded", self.store.state("event_processing_error"))
        self.assertEqual(len(News(self.store, http).news_store.records("NVDA", self.now)), 1)

    def test_roundup_cannot_attribute_another_issuers_guidance_to_nvda_even_from_old_cache(self):
        from trabe.news_events import encoded

        headline = "Four stocks meeting the screening criteria are highlighted: Expedia Group with raised 2026 guidance, Ralph Lauren with improved fiscal 2027 outlook, NVIDIA with strong product momentum despite leadership transition, and Lam Research with robust guidance and AI-driven demand."
        item = self.item(headline, "roundup")
        result = analyze(item, "NVDA")
        self.assertEqual(result["direction"], "unknown")
        self.assertEqual(result["materiality"], "low")
        self.assertFalse(result["push_eligible"])
        # Immutable old analyses remain stored, but cached presentation must be safe.
        old = result | {
            "type": "results",
            "direction": "positive",
            "materiality": "high",
            "action": "guidance change",
            "push_eligible": True,
            "issuers": ["NVDA"],
            "seeded": False,
            "correction": False,
        }
        with self.store.connection() as db:
            db.execute(
                "INSERT INTO news_events VALUES ('old','signature',?,?,?,'high',0)",
                (self.now, self.now, self.now - 60),
            )
            db.execute(
                "INSERT INTO event_revisions VALUES ('revision','old','facts',?,?)",
                (self.now, encoded(old)),
            )
            db.execute(
                "INSERT INTO event_evidence VALUES ('evidence','fixture','item','https://example.com','content',?,?,?)",
                (self.now - 60, self.now, encoded({"publisher": "Fixture"})),
            )
            db.execute("INSERT INTO event_bindings VALUES ('old','revision','evidence')")
        projected = self.repo.events(["NVDA"], self.now)[0]
        self.assertEqual(projected["direction"], "unknown")
        self.assertFalse(projected["push_eligible"])
        self.assertEqual(NewsDigest(self.store).material(["NVDA"], self.now), [])
        from trabe.news_push import NewsPush

        self.store.add("NVDA", 2)
        self.store.set_setting("news_enabled", "on")
        self.store.set_setting("news_alerts_enabled", "on")
        generation = int(self.store.state("news_event_generation"))
        with self.store.connection() as db:
            outbox_id = Outbox(self.store).send(
                123, "Old cached roundup", kind="news_event", db=db, now=self.now
            )
            db.execute(
                "INSERT INTO event_deliveries VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "queued-case",
                    "old",
                    "revision",
                    123,
                    generation,
                    "queued",
                    "",
                    self.now,
                    self.now + 86400,
                    self.now,
                    "2026-11-14",
                    outbox_id,
                ),
            )
            self.assertEqual(NewsPush.flush_policy(self.store, db, outbox_id, self.now), "drop")
            self.assertEqual(
                db.execute(
                    "SELECT reason FROM event_deliveries WHERE semantic_key='queued-case'"
                ).fetchone()[0],
                "attribution_unresolved",
            )

    def test_presentation_status_write_failure_cannot_fail_legacy_refresh(self):
        from test_news import FakeHTTP

        http = FakeHTTP()
        http.data["NVDA"] = [
            {
                "headline": "NVIDIA raises guidance",
                "summary": "",
                "url": "https://example.com/input",
                "datetime": self.now - 60,
                "related": "NVDA",
                "id": "one",
            }
        ]
        original = self.store.set_state

        def write(key, value):
            if key == "event_processing_error":
                raise RuntimeError("fixture locked status")
            return original(key, value)

        with (
            patch.object(EventStore, "ingest", side_effect=RuntimeError("fixture locked audit")),
            patch.object(self.store, "set_state", side_effect=write),
        ):
            result = News(self.store, http).refresh(["NVDA"], now=self.now)
        self.assertEqual(result["refreshed"], 1)
        self.assertEqual(result["failed"], 0)

    def test_identical_provider_responses_have_identical_financial_inputs_with_mirror_off_on(self):
        from datetime import date

        from test_news import FakeHTTP

        from trabe.advisor import Advisor
        from trabe.calendar import USCalendar

        inputs = []
        for mirror in (False, True):
            with tempfile.TemporaryDirectory() as directory:
                store = Store(Path(directory) / "isolation.db")
                store.add("VST", 2)
                http = FakeHTTP()
                http.data["VST"] = [
                    {
                        "headline": "Vistra raises guidance",
                        "summary": "Vistra raises guidance.",
                        "url": "https://example.com/input",
                        "datetime": self.now - 60,
                        "related": "VST",
                        "id": "one",
                    }
                ]
                news = News(store, http)
                if mirror:
                    news.refresh(["VST"], now=self.now)
                else:
                    with patch.object(news, "_mirror", return_value=False):
                        news.refresh(["VST"], now=self.now)
                with store.connection() as db:
                    inputs.append(
                        Advisor(store).evidence_inputs(
                            db, "VST", self.now, USCalendar().session(date(2026, 11, 16))
                        )
                    )
        self.assertEqual(inputs[0], inputs[1])
