"""Company-specific analysis and immutable source occurrences, using fixtures."""

import json
import tempfile
import unittest
from pathlib import Path

from test_news import FakeHTTP

from trabe.news import News
from trabe.news_analysis import RulesAnalyzer
from trabe.news_quality import context
from trabe.news_sources import vendor_score
from trabe.store import Store


class NewsQualityTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = Store(Path(directory.name) / "news.db")
        self.news = News(self.store, FakeHTTP())
        self.now = 1793793600.0

    def item(
        self,
        headline="Vistra raises guidance",
        *,
        provider="finnhub",
        identity="1",
        snippet="",
        **extra,
    ):
        return {
            "provider": provider,
            "item_id": identity,
            "url": "https://example.com/" + identity,
            "headline": headline,
            "snippet": snippet,
            "published_at": self.now - 60,
            "publisher": "Fixture",
            "input_quality": "snippet" if snippet else "headline_only",
            **extra,
        }

    def test_sixty_grouped_synthetic_acceptance_examples(self):
        data = json.loads((Path(__file__).parent / "fixtures/news_quality.json").read_text())
        self.assertEqual(len(data["cases"]), 60)
        family_splits = {}
        for example in data["cases"]:
            family_splits.setdefault(example["family"], set()).add(example["split"])
            with self.subTest(headline=example["headline"], ticker=example["ticker"]):
                result = RulesAnalyzer().analyze(
                    self.item(example["headline"]), context(example["ticker"])
                )
                self.assertEqual(result["label"], example["expected"])
        self.assertTrue(all(len(splits) == 1 for splits in family_splits.values()))

    def test_revision_occurrences_and_late_company_association(self):
        original = self.item("Alphabet and Vistra raise guidance")
        self.news._merge([original], "GOOGL", "finnhub", self.now)
        self.news._merge(
            [original | {"snippet": "A changed report"}], "GOOGL", "finnhub", self.now + 1
        )
        self.news._merge([original], "GOOGL", "finnhub", self.now + 2)
        self.news._merge([original], "VST", "finnhub", self.now + 10)
        with self.store.connection() as db:
            revisions = db.execute(
                "SELECT id,input_hash FROM news_revisions ORDER BY id"
            ).fetchall()
            reused = db.execute(
                "SELECT reused_analysis_id FROM news_analyses WHERE revision_id=? AND ticker='GOOGL'",
                (revisions[-1][0],),
            ).fetchone()[0]
        self.assertEqual(len(revisions), 3)
        self.assertEqual(revisions[0][1], revisions[2][1])
        self.assertIsNotNone(reused)
        self.assertEqual(self.news.news_store.records("VST", self.now + 3), [])
        self.assertEqual(len(self.news.news_store.records("VST", self.now + 11)), 1)

    def test_provider_variants_and_score_changes_do_not_overwrite_each_other(self):
        first = self.item()
        other = self.item(
            provider="massive",
            snippet="Vistra raises its outlook.",
            annotations=[vendor_score("massive", "VST", "negative")],
        )
        self.news._merge([first], "VST", "finnhub", self.now)
        self.news._merge([other], "VST", "massive", self.now + 1)
        self.news._merge([first], "VST", "finnhub", self.now + 2)
        self.news._merge(
            [other | {"annotations": [vendor_score("massive", "VST", "positive")]}],
            "VST",
            "massive",
            self.now + 3,
        )
        with self.store.connection() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM news_revisions").fetchone()[0], 2)
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM news_provider_observations").fetchone()[0], 3
            )
        before = self.news.news_store.records("VST", self.now + 2)
        remote = next(r for r in before if r["provider"] == "massive")["vendor_scores"][0]
        self.assertEqual(remote["direction"], "negative")
        self.assertIsNone(remote["bias"])
        view, _ = self.news._compute("VST", self.now + 4)
        self.assertEqual(view["event_count"], 1)
        self.assertEqual(view["second_view"]["state"], "agrees on direction")

    def test_duplicate_events_keep_age_but_material_numbers_stay_separate(self):
        first = self.item("Vistra reports EPS", snippet="Vistra reports EPS 1.20 vs expected 1.00")
        copy = first | {
            "url": "https://other.example/copy",
            "item_id": "2",
            "published_at": self.now - 1,
        }
        changed = first | {
            "url": "https://other.example/corrected",
            "item_id": "3",
            "snippet": "Vistra reports EPS 0.50 vs expected 1.00",
        }
        for item in (first, copy, changed):
            self.news._merge([item], "VST", "finnhub", self.now)
        view, groups = self.news._compute("VST", self.now)
        self.assertEqual(len(groups), 2)
        self.assertEqual(max(len(g["members"]) for g in groups), 2)
        self.assertTrue(view["mixed"])
        duplicate = next(g for g in groups if len(g["members"]) == 2)
        self.assertEqual(duplicate["published_at"], self.now - 60)

    def test_missing_is_null_and_repeated_phrases_do_not_amplify_bias(self):
        analyzer = RulesAnalyzer()
        unknown = analyzer.analyze(self.item("A company update"), context("ON"))
        self.assertIsNone(unknown["bias"])
        self.assertIsNone(analyzer.analyze(self.item("Vistra"), context("VST"))["bias"])
        result = analyzer.analyze(
            self.item(snippet="Vistra raises guidance. The company raised guidance."),
            context("VST"),
        )
        self.assertEqual(result["bias"], 0.5)
        self.news._merge(
            [self.item("Vistra beats expectations but cuts guidance")], "VST", "finnhub", self.now
        )
        view, _ = self.news._compute("VST", self.now)
        self.assertEqual(view["bias"], 0)
        self.assertTrue(view["mixed"])

    def test_analysis_rejects_unbounded_or_ungrounded_results(self):
        item = self.item()
        revision, _ = self.news.news_store.observe(item, "VST", self.now)
        result = RulesAnalyzer().analyze(item, context("VST"))
        with self.assertRaises(ValueError):
            self.news.news_store.save_analysis(
                revision, "VST", result | {"bias": float("nan")}, self.now
            )
        with self.assertRaises(ValueError):
            self.news.news_store.save_analysis(
                revision,
                "VST",
                result | {"supporting_spans": [{"text": "invented profit"}]},
                self.now,
            )

    def test_explicit_correction_supersedes_only_after_receipt(self):
        original = self.item()
        self.news._merge([original], "VST", "finnhub", self.now)
        correction = self.item("Correction: Vistra cuts guidance", identity="2", correction_of="1")
        self.news._merge([correction], "VST", "finnhub", self.now + 10)
        before = self.news.news_store.records("VST", self.now + 1)
        after = self.news.news_store.records("VST", self.now + 11)
        self.assertEqual(len(before), 1)
        self.assertEqual(before[0]["analysis"]["label"], "Bullish")
        self.assertEqual(len(after), 1)
        self.assertEqual(after[0]["analysis"]["label"], "Bearish")

    def test_republishing_identical_content_does_not_refresh_event_age(self):
        original = self.item()
        self.news._merge([original], "VST", "finnhub", self.now)
        self.news._merge(
            [original | {"published_at": self.now + 3600}], "VST", "finnhub", self.now + 3601
        )
        view, _ = self.news._compute("VST", self.now + 3602)
        self.assertEqual(view["evidence"][0]["published_at"], self.now - 60)
        self.assertEqual(view["evidence"][0]["known_at"], self.now + 3601)
        self.news._merge(
            [original | {"headline": "Vistra cuts guidance", "published_at": self.now + 3603}],
            "VST",
            "finnhub",
            self.now + 3604,
        )
        self.news._merge(
            [original | {"published_at": self.now + 7200}], "VST", "finnhub", self.now + 7201
        )
        returned, _ = self.news._compute("VST", self.now + 7202)
        self.assertEqual(returned["evidence"][0]["published_at"], self.now + 7200)

    def test_schema_five_import_preserves_legacy_state_without_backdating_inputs(self):
        self.news.repo.put(
            "news",
            "old",
            "VST",
            "finnhub",
            {
                "canonical_url": "https://example.com/legacy",
                "headline": "Vistra raises guidance",
                "summary": "Legacy extract",
                "published_at": self.now - 60,
                "symbols": ["VST"],
            },
            now=self.now - 50,
        )
        self.store.add("VST", 2)
        self.store.set_state("telegram_offset", "42")
        with self.store.connection() as db:
            db.execute("PRAGMA user_version=5")
        from unittest.mock import patch

        with patch("trabe.news_store.time.time", return_value=self.now):
            reopened = Store(self.store.path)
        rows = News(reopened, FakeHTTP()).news_store.records("VST", self.now)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["input_quality"], "legacy_extract")
        self.assertEqual(rows[0]["observed_at"], self.now)
        self.assertEqual(reopened.state("telegram_offset"), "42")
        self.assertEqual(reopened.watchlist(), [("VST", 2)])
        with reopened.connection() as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 7)

    def test_cached_analysis_is_reused_without_backdating_new_occurrences(self):
        from unittest.mock import patch

        analyzer = RulesAnalyzer()
        first = self.item()
        with patch.object(RulesAnalyzer, "analyze", side_effect=analyzer.analyze) as analyze:
            self.news._merge([first], "VST", "finnhub", self.now)
            self.news._merge([first], "VST", "finnhub", self.now + 1)
            self.news._merge(
                [first | {"headline": "Vistra cuts guidance"}], "VST", "finnhub", self.now + 2
            )
            self.news._merge([first], "VST", "finnhub", self.now + 3)
        self.assertEqual(analyze.call_count, 2)
        row = self.news.news_store.records("VST", self.now + 4)[0]
        self.assertEqual(row["bound_at"], self.now + 3)
        self.assertEqual(row["analysis"]["label"], "Bullish")


if __name__ == "__main__":
    unittest.main()
