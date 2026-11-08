"""Shared article reuse, strict earnings evidence and stable company-news inputs."""

import copy
import tempfile
import unittest
from pathlib import Path

from test_earnings_news import NOW, article

from trabe.articles import ArticleCache, parse_document, sentences
from trabe.earnings_news import EarningsNews, extract_document
from trabe.news import News
from trabe.news_sources import vendor_score
from trabe.signals import SignalStore, WorkStopped
from trabe.snapshots import SnapshotService
from trabe.store import Store

URL = "https://finance.yahoo.com/news/vistra-earnings-example.html"


class ArticleTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.store = Store(Path(temp.name) / "agent.db")
        self.store.add("VST", 2)
        self.body = article(
            "Vistra reported strong earnings and revenue growth. Basic EPS (Q3 2026 vs Q3 2025): US$3.25 vs. US$1.78. Vistra raised its guidance for the current year."
        )
        fixture = self

        class HTTP:
            def __init__(self):
                self.body_calls = 0

            def xml(self, url, provider):
                if "feeds.finance.yahoo.com" in url:
                    return f"<rss><channel><item><title>Vistra (VST) Q3 earnings</title><pubDate>Sat, 07 Nov 2026 02:12:31 GMT</pubDate><link>{URL}</link></item></channel></rss>".encode()
                self.body_calls += 1
                return fixture.body

            def finnhub(self, endpoint, **params):
                return [
                    {
                        "id": 1,
                        "headline": "Vistra (VST) Q3 earnings",
                        "summary": "Vistra reported results.",
                        "datetime": NOW - 3600,
                        "source": "Fixture",
                        "url": URL,
                        "related": "VST",
                    }
                ]

        self.http = HTTP()

    def test_document_cache_is_reused_by_news_and_strict_earnings_parser(self):
        News(self.store, self.http).refresh(["VST"], now=NOW)
        repo = SignalStore(self.store)
        repo.put(
            "earnings",
            "quarter",
            "VST",
            "finnhub",
            {"date": "2026-11-06", "period": "FY2026 Q3", "comparisons": {}},
            now=NOW,
            event_time=NOW,
        )
        EarningsNews(self.store, self.http).refresh("VST", now=NOW + 1)
        value = repo.get("earnings", "quarter")["payload"]["comparisons"]["eps"]
        self.assertEqual(value["actual"], "3.25")
        self.assertIsNone(value["estimate"])
        self.assertEqual(value["article_received_at"], NOW)
        self.assertEqual(self.http.body_calls, 1)

    def test_feed_and_article_revisions_stay_stable_and_copies_count_once(self):
        news = News(self.store, self.http)
        news.refresh(["VST"], now=NOW)
        first = news._compute("VST", NOW)[0]
        self.assertEqual(first["event_count"], 1)
        self.assertEqual(first["evidence"][0]["input_quality"], "article")
        packet = SnapshotService(self.store).capture(now=NOW)
        news.refresh(["VST"], now=NOW + 1, force=True)
        news.refresh(["VST"], now=NOW + 2, force=True)
        with self.store.connection() as db:
            providers = db.execute(
                "SELECT provider,COUNT(*) FROM news_revisions GROUP BY provider"
            ).fetchall()
        self.assertEqual(dict(providers), {"finnhub": 1, "yahoo-rss": 1, "yahoo-article": 1})
        self.assertEqual(self.http.body_calls, 1)
        self.assertEqual(SnapshotService(self.store).repo.get(), packet)
        news.refresh(["VST"], now=NOW + 6 * 3600 + 1, force=True)
        with self.store.connection() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM news_revisions").fetchone()[0], 3)
        self.assertEqual(self.http.body_calls, 2)
        text = news.report(["VST"], now=NOW + 2)
        self.assertIn("Article extract:", text)
        self.assertIn("Vistra reported strong earnings", text)
        self.assertNotIn("https://", text)

    def test_vendor_annotation_stays_bound_to_its_original_feed_observation(self):
        news = News(self.store, self.http)
        news.refresh(["VST"], now=NOW)
        item = news._normalize(self.http.finnhub("company-news")[0], "VST", "massive", NOW)
        item["annotations"] = [vendor_score("massive", "VST", "positive")]
        news._merge([item], "VST", "massive", NOW)
        view, groups = news._compute("VST", NOW)
        self.assertEqual(view["event_count"], 1)
        rows = groups[0]["members"]
        self.assertTrue(next(r for r in rows if r["provider"] == "massive")["vendor_scores"])
        self.assertEqual(
            next(r for r in rows if r["provider"] == "yahoo-article")["vendor_scores"], []
        )

    def test_failed_article_keeps_feed_evidence_and_negative_cache_avoids_retry(self):
        def offline(*args, **kwargs):
            if "feeds.finance.yahoo.com" not in args[0]:
                self.http.body_calls += 1
            raise OSError("private diagnostic")

        self.http.xml = offline
        news = News(self.store, self.http)
        news.refresh(["VST"], now=NOW)
        news.refresh(["VST"], now=NOW + 1, force=True)
        self.assertEqual(self.http.body_calls, 1)
        self.assertIn("Feed extract:", news.report(["VST"], now=NOW + 1))
        self.assertNotIn("private diagnostic", news.report(["VST"], now=NOW + 1))

    def test_cache_fetch_and_storage_bounds_and_unsupported_urls(self):
        cache = ArticleCache(self.store, self.http)
        self.assertIsNone(cache.get("https://example.com/private.html", NOW))
        self.assertIsNone(cache.get("https://finance.yahoo.com/quote/VST", NOW))
        cache.get(URL, NOW)
        cache.get(URL.replace("example", "second"), NOW)
        self.assertIsNone(cache.get(URL.replace("example", "third"), NOW))
        self.assertEqual(self.http.body_calls, 2)
        cache = ArticleCache(self.store, self.http, max_fetches=40)
        for i in range(34):
            cache.get(f"https://finance.yahoo.com/news/story-{i}.html", NOW + i)
        with self.store.connection() as db:
            rows = db.execute(
                "SELECT payload FROM signal_http_cache WHERE provider='yahoo-article'"
            ).fetchall()
        self.assertEqual(len(rows), 32)
        self.assertTrue(all(len(row[0].encode()) <= 128 * 1024 for row in rows))

    def test_future_document_scripts_and_conflicting_eps_remain_unusable(self):
        body = article(
            extra="<svg>Basic EPS (Q3 2026 vs Q3 2025): US$99</svg><script>Vistra raises guidance</script>"
        )
        doc = parse_document(body, NOW)
        self.assertNotIn("99", doc["text"])
        self.assertNotIn("raises guidance", doc["text"])
        future = copy.deepcopy(doc) | {"received_at": NOW + 1}
        self.assertIsNone(extract_document(future, "VST", "FY2026 Q3", "2026-11-06", NOW))
        self.assertIsNone(parse_document(article(published="2026-11-10T00:00:00Z"), NOW))
        self.assertIsNone(parse_document(b"<article>No publication metadata</article>", NOW))
        self.body = article(
            "Basic EPS (Q3 2026 vs Q3 2025): US$3.25; Basic EPS (Q3 2026 vs Q3 2025): US$1.75"
        )
        doc = ArticleCache(self.store, self.http).get(URL, NOW)
        self.assertIsNone(extract_document(doc, "VST", "FY2026 Q3", "2026-11-06", NOW))

    def test_future_cache_is_excluded_and_pause_does_not_record_failure(self):
        ArticleCache(self.store, self.http).get(URL, NOW)
        self.assertIsNone(ArticleCache(self.store, self.http).get(URL, NOW - 1))
        self.assertEqual(self.http.body_calls, 1)

        def stop(*args, **kwargs):
            raise WorkStopped("paused")

        self.http.xml = stop
        with self.assertRaises(WorkStopped):
            ArticleCache(self.store, self.http).get(URL.replace("example", "paused"), NOW)
        with self.store.connection() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM signal_http_cache").fetchone()[0], 1)

    def test_disabling_yahoo_news_prevents_article_fetches_from_other_feed_links(self):
        self.store.set_setting("news_sources", "finnhub")
        news = News(self.store, self.http)
        news.refresh(["VST"], now=NOW)
        self.assertEqual(self.http.body_calls, 0)
        self.assertEqual(news._compute("VST", NOW)[0]["evidence"][0]["input_quality"], "snippet")

    def test_new_feed_correction_outranks_an_older_cached_article(self):
        news = News(self.store, self.http)
        news.refresh(["VST"], now=NOW)
        original = self.http.finnhub("company-news")[0]
        self.http.finnhub = lambda *a, **kw: [
            original
            | {
                "headline": "Vistra cuts guidance after weak earnings",
                "summary": "Vistra now expects lower revenue.",
            }
        ]
        news.refresh(["VST"], now=NOW + 2, force=True)
        evidence = news._compute("VST", NOW + 2)[0]["evidence"][0]
        self.assertIn("cuts guidance", evidence["headline"])
        self.assertEqual(evidence["input_quality"], "snippet")
        self.assertEqual(self.http.body_calls, 1)

    def test_older_article_outside_leading_stories_has_dated_context_in_report_and_briefing(self):
        news = News(self.store, self.http)
        original = self.http.finnhub("company-news")[0]
        self.http.finnhub = lambda *a, **kw: (
            [original]
            + [
                original
                | {
                    "id": i,
                    "headline": f"Vistra raises guidance after contract {i}",
                    "summary": f"Vistra revenue growth exceeds {i} percent.",
                    "url": f"https://example.com/new-{i}",
                    "datetime": NOW - i,
                }
                for i in range(2, 6)
            ]
        )
        news.refresh(["VST"], now=NOW)
        view = news._compute("VST", NOW)[0]
        self.assertTrue(all(i["input_quality"] != "article" for i in view["evidence"]))
        report = news.report(["VST"], now=NOW)
        self.assertIn("Additional article context", report)
        self.assertIn("Article extract:", report)
        brief = "\n".join(news.briefing(["AMD", "AMZN", "GOOGL", "NVDA", "VST"], now=NOW))
        self.assertIn("VST • Additional article context", brief)

    def test_extract_omits_publisher_author_and_read_time_header(self):
        document = {
            "text": "Vistra earnings report Jane Reporter Fri, November 6, 2026 at 1:44 PM GMT+1 2 min read VST. Vistra raised its guidance after strong profit growth. Vistra added new generation capacity during the quarter."
        }
        from trabe.news_quality import context

        excerpt = sentences(document, context("VST"))
        self.assertIn("Vistra raised its guidance", excerpt)
        self.assertNotIn("Jane Reporter", excerpt)
        self.assertNotIn("min read", excerpt)


if __name__ == "__main__":
    unittest.main()
