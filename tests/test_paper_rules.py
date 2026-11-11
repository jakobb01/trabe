"""Published paper-rule thresholds, source gates and whole-share target behavior."""

import copy
import unittest
from datetime import date
from decimal import Decimal

from test_price_endpoints import chart

from trabe.calendar import USCalendar
from trabe.paper_rules import classify, whole
from trabe.price_endpoints import parse_endpoint


def inputs_for(session, *, ticker="VST", bias="0.4", price="50"):
    calendar = USCalendar()
    previous = calendar.previous(date.fromisoformat(session.day))
    now = session.opens - 900
    reference = parse_endpoint(
        chart(ticker, previous, closing=price),
        symbol=ticker,
        session=previous,
        edge="close",
        received_at=previous.closes + 1800,
    )
    return {
        "ticker": ticker,
        "as_of": now,
        "previous_session": previous.day,
        "next_session": calendar.next_session(session.closes).day,
        "reference": reference,
        "news": [
            {
                "group_id": str(i),
                "published_at": now - 60,
                "known_at": now - 30,
                "source_published_at": now - 60,
                "input_quality": "snippet",
                "snippet": "Company evidence.",
                "analysis": {"bias": bias, "relevance": "direct"},
            }
            for i in range(2)
        ],
        "news_sources": [{"succeeded_at": now - 30, "error": ""}],
        "fundamental_sources": [
            {"kind": k, "succeeded_at": now - 30, "error": ""} for k in ("earnings", "insiders")
        ],
        "earnings": [],
        "insiders": [],
        "insider_min_value_usd": "10000",
        "market_outlook": {
            "id": "market",
            "session_date": session.day,
            "as_of": now - 900,
            "gaps": [],
            "forecast": {"session": "mixed"},
        },
        "watchlist_member": True,
        "corporate_actions": [],
    }


def earnings(inputs, actual="2", estimate="1"):
    now = inputs["as_of"]
    provenance = {
        "period": "FY2026 Q3",
        "basis": "adjusted diluted",
        "currency": "USD",
        "source": "fixture",
        "source_url": "https://example.com/result",
        "known_at": now - 300,
    }
    inputs["earnings"] = [
        {
            "key": "q3",
            "known_at": now - 300,
            "source": "fixture",
            "facts": {
                "date": inputs["previous_session"],
                "period": "FY2026 Q3",
                "reported": True,
                "comparisons": {
                    "eps": {
                        "actual": actual,
                        "estimate": estimate,
                        "source": "fixture",
                        "actual_provenance": dict(provenance),
                        "estimate_provenance": dict(provenance),
                    }
                },
            },
        }
    ]


def insider(inputs):
    inputs["insiders"] = [
        {
            "known_at": inputs["as_of"] - 300,
            "facts": {
                "filing_id": "filing",
                "accession": "filing",
                "transaction_date": inputs["previous_session"],
                "published_at": inputs["as_of"] - 300,
                "eligibility_errors": [],
                "transaction_code": "P",
                "derivative_status": "non-derivative",
                "amendment": False,
                "value_usd": "20000",
            },
        }
    ]


class RulesTests(unittest.TestCase):
    def setUp(self):
        self.session = USCalendar().session(date(2026, 11, 10))
        self.inputs = inputs_for(self.session)

    def classify(self, inputs=None, qty=0):
        return classify(
            self.inputs if inputs is None else inputs, {"VST": {"quantity": qty}}, self.session
        )

    def test_P10_explicit_threshold_vectors_and_all_components(self):
        self.assertEqual((self.classify()["score"], self.classify()["tier"]), (2, "small"))
        earnings(self.inputs)
        self.assertEqual((self.classify()["score"], self.classify()["tier"]), (3, "medium"))
        insider(self.inputs)
        self.assertEqual((self.classify()["score"], self.classify()["tier"]), (4, "big"))
        self.inputs["market_outlook"]["forecast"]["session"] = "bullish"
        self.assertEqual(self.classify()["components"], {"N": 2, "E": 1, "I": 1, "M": 1})
        self.inputs = inputs_for(self.session, bias="-0.2")
        earnings(self.inputs, "1", "2")
        self.assertEqual(self.classify(qty=4)["action"], "sell")
        self.inputs = inputs_for(self.session, bias="0")
        insider(self.inputs)
        self.assertEqual(self.classify(qty=4)["action"], "hold")

    def test_P03_P04_P14_targets_preserve_neutral_hold_and_limit_whole_shares(self):
        self.assertEqual(self.classify(qty=4)["action"], "hold")
        self.assertEqual(self.classify(qty=12)["action"], "sell")
        self.inputs["reference"]["price"] = "250"
        self.assertEqual(self.classify()["action"], "hold")
        for qty in (True, 1.5, Decimal("1"), float("nan"), -1):
            with self.assertRaises(ValueError):
                whole(qty)

    def test_P11_required_news_gaps_duplicate_future_and_bad_bias(self):
        for mode in ("duplicate", "future", "stale", "failed", "nan", "relevance"):
            inputs = copy.deepcopy(self.inputs)
            if mode == "duplicate":
                inputs["news"][1] = inputs["news"][0]
            elif mode == "future":
                inputs["news"][0]["known_at"] += 1000
            elif mode == "stale":
                inputs["news_sources"][0]["succeeded_at"] -= 20000
            elif mode == "failed":
                inputs["news_sources"][0]["error"] = "failed"
            elif mode == "nan":
                inputs["news"][0]["analysis"]["bias"] = "NaN"
            else:
                inputs["news"][0]["analysis"]["relevance"] = "contextual"
            self.assertEqual(self.classify(inputs)["action"], "hold", mode)
        for item in self.inputs["news"]:
            item["conflict"] = True
        self.assertEqual(self.classify()["components"]["N"], 0)

    def test_P12_provenance_mismatches_withdrawals_and_unpaired_actuals_are_neutral(self):
        earnings(self.inputs)
        original = copy.deepcopy(self.inputs)
        for mode in (
            "basis",
            "period",
            "currency",
            "withdrawn",
            "no_estimate",
            "alpha",
            "missing_proof",
        ):
            inputs = copy.deepcopy(original)
            fact = inputs["earnings"][0]["facts"]
            metric = fact["comparisons"]["eps"]
            if mode in {"basis", "period", "currency"}:
                metric["estimate_provenance"][mode] = "mismatched"
            elif mode == "withdrawn":
                fact["withdrawn_results"] = {"eps": True}
            elif mode == "no_estimate":
                metric["estimate"] = None
            elif mode == "alpha":
                metric["source"] = "alphavantage"
            else:
                metric.pop("actual_provenance")
            self.assertEqual(self.classify(inputs)["components"]["E"], 0, mode)

    def test_P12_earnings_date_blocks_buys_but_not_reductions(self):
        self.inputs["earnings"] = [
            {
                "known_at": self.inputs["as_of"] - 10,
                "facts": {"date": self.inputs["next_session"], "reported": False},
            }
        ]
        self.assertEqual(self.classify()["action"], "hold")
        self.assertIn("earnings_soon", self.classify()["reasons"])
        self.assertEqual(self.classify(qty=12)["action"], "sell")

    def test_P13_qualified_insiders_and_rejected_coverage(self):
        insider(self.inputs)
        self.assertEqual(self.classify()["components"]["I"], 1)
        original = copy.deepcopy(self.inputs)
        for key, value in (
            ("eligibility_errors", ["unqualified"]),
            ("derivative_status", "derivative"),
            ("amendment", True),
            ("published_at", self.inputs["as_of"] + 1),
        ):
            inputs = copy.deepcopy(original)
            inputs["insiders"][0]["facts"][key] = value
            self.assertEqual(self.classify(inputs)["components"]["I"], 0)
        self.inputs["insiders"] = []
        self.assertNotIn("insiders_coverage_unavailable", self.classify()["gaps"])
        self.inputs["fundamental_sources"][1]["succeeded_at"] = None
        self.assertIn("insiders_coverage_unavailable", self.classify()["gaps"])

    def test_P14_optional_cap_never_causes_a_sale(self):
        earnings(self.inputs)
        self.inputs["market_outlook"] = None
        result = self.classify()
        self.assertEqual((result["action"], result["tier"]), ("buy", "small"))
        # Six shares are below the uncapped $400 target, above the capped $200 target.
        self.assertEqual(self.classify(qty=6)["action"], "hold")
        self.inputs["market_outlook"] = {
            "session_date": self.session.day,
            "as_of": self.inputs["as_of"] + 1,
            "forecast": {"session": "bullish"},
        }
        self.assertEqual(self.classify()["components"]["M"], 0)

    def test_P15_removed_ticker_cannot_buy_and_missing_reference_holds(self):
        self.inputs["watchlist_member"] = False
        self.assertEqual(self.classify()["action"], "hold")
        self.assertEqual(self.classify(qty=12)["action"], "sell")
        self.inputs["reference"] = None
        self.assertIn("reference_missing", self.classify(qty=12)["gaps"])
        self.assertEqual(self.classify(qty=12)["action"], "hold")

    def test_P26_known_actions_and_nonstock_identity(self):
        self.inputs["corporate_actions"] = [{"kind": "split", "at": self.session.opens - 100}]
        self.assertEqual(self.classify(qty=12)["action"], "hold")
        self.inputs["corporate_actions"] = []
        self.inputs["reference"]["instrument_type"] = "ETF"
        self.assertIn("unsupported_security", self.classify()["reasons"])
