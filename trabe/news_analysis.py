"""Deterministic, target-specific baseline; no inference/model dependency."""

import json
import re
from decimal import Decimal
from typing import Protocol

from trabe.news_quality import ALIASES, context, digest, event_type, label, mentions, relevance

METHOD = "rules-v2"
POSITIVE = (
    r"beat(?:s)? (?:earnings |analyst )?(?:estimates|expectations)",
    r"(?:raise[sd]?|raising) (?:its )?(?:guidance|forecast|outlook)",
    r"(?:record|rising|higher|strong) (?:profit|revenue|sales|earnings|demand)",
    r"(?:profit|revenue|sales|earnings) (?:growth|rise[sd]?|increase[sd]?)",
    r"upgrad(?:e[sd]?|ing)",
    r"buyback[ s]?",
    r"(?:wins?|receives?) (?:regulatory )?approval",
)
NEGATIVE = (
    r"miss(?:es)? (?:earnings |analyst )?(?:estimates|expectations)",
    r"(?:cut[ s]?|lower(?:s|ed|ing)?) (?:its )?(?:guidance|forecast|outlook)",
    r"(?:falling|lower|weak) (?:profit|revenue|sales|earnings|demand)",
    r"(?:profit|revenue|sales|earnings) (?:decline[sd]?|fall[ s]?|drop[ s]?)",
    r"downgrad(?:e[sd]?|ing)",
    r"lawsuit|investigation|recall|bankruptcy|default|layoffs",
    r"(?:regulatory )?rejection",
)


class NewsAnalyzer(Protocol):
    method: str

    def analyze(self, item: dict, company: dict) -> dict: ...


class RulesAnalyzer:
    method = METHOD

    @staticmethod
    def input_hash(item, company):
        return digest(
            json.dumps(
                {
                    "headline": item["headline"],
                    "snippet": item.get("snippet", ""),
                    "company": company,
                    "method": METHOD,
                    "quality": item.get("input_quality"),
                },
                sort_keys=True,
                ensure_ascii=False,
            )
        )

    def analyze(self, item, company):
        headline, snippet = item["headline"], item.get("snippet", "")
        supplied = headline + "\n" + snippet
        rel, reason = relevance(headline, snippet, company)
        kind, importance = event_type(supplied)
        evidence, seen, indicators = [], set(), set()
        clauses = re.split(
            r"(?:\n|(?<=[.!?;])\s+|\bbut\b|\bhowever\b|\bwhile\b)", supplied, flags=re.I
        )
        subject_is_target = rel == "contextual"
        mention_cache = {}
        other_companies = [
            context(t)
            for t in ALIASES
            if t != company["ticker"] and not {t, company["ticker"]} <= {"GOOG", "GOOGL"}
        ]
        for clause in clauses:
            normalized = " ".join(clause.casefold().split())
            if normalized not in mention_cache:
                mention_cache[normalized] = (
                    mentions(clause, company),
                    any(mentions(clause, other) for other in other_companies),
                )
            target, others = mention_cache[normalized]
            if target:
                subject_is_target = True
            elif others:
                subject_is_target = False
            if rel == "direct" and not target:
                if not subject_is_target:
                    continue
            if normalized in seen:
                continue
            seen.add(normalized)
            for patterns, sign in ((POSITIVE, 1), (NEGATIVE, -1)):
                for index, pattern in enumerate(patterns):
                    for match in re.finditer(r"\b(?:" + pattern + r")\b", clause, re.I):
                        before = re.findall(
                            r"[a-z']+", clause[max(0, match.start() - 40) : match.start()].lower()
                        )
                        negated = any(
                            w in {"not", "no", "never", "without", "denies"} for w in before[-3:]
                        )
                        identity = (sign, index)
                        if not negated and identity not in indicators:
                            evidence.append({"text": match.group(), "sign": sign})
                            indicators.add(identity)
            comparison = re.search(
                r"\bEPS\s*\$?(-?\d+(?:\.\d+)?)\s*(?:vs\.?|versus)\s*(?:an? )?(?:expected|estimated|consensus)\s*\$?(-?\d+(?:\.\d+)?)",
                clause,
                re.I,
            )
            if comparison:
                actual, expected = map(Decimal, comparison.groups())
                sign = (actual > expected) - (actual < expected)
                if sign and (sign, 0) not in indicators:
                    evidence.append({"text": comparison.group(), "sign": sign})
                    indicators.add((sign, 0))
        bias = (
            round(sum(e["sign"] for e in evidence) / max(2, len(evidence)), 3)
            if rel in ("direct", "contextual")
            else None
        )
        if not evidence and len(re.findall(r"[A-Za-z0-9]+", supplied)) < 3:
            bias = None
            reason = "Insufficient supplied news beyond the issuer name"
        signs = {e["sign"] for e in evidence}
        return {
            "relevance": rel,
            "reason": "; ".join(e["text"] for e in evidence[:2]) if evidence else reason,
            "relevance_reason": reason,
            "event_type": kind,
            "importance": importance,
            "bias": bias,
            "label": label(bias),
            "mixed": len(signs) > 1,
            "supporting_spans": evidence[:12],
            "method": METHOD,
            "schema": 1,
            "context_version": company["version"],
            "company": company,
            "input_hash": digest(
                json.dumps(
                    {
                        "headline": headline,
                        "snippet": snippet,
                        "company": company,
                        "method": METHOD,
                        "quality": item.get("input_quality"),
                    },
                    sort_keys=True,
                    ensure_ascii=False,
                )
            ),
        }
