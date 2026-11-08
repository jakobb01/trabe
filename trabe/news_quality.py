"""Bounded, pure company relevance, event grouping and news aggregation."""

import hashlib
import math
import re

VERSION = "quality-v2"
WINDOW = 72 * 3600
ALIASES_VERSION = "issuer-aliases-v1"
ALIASES = {
    "GOOGL": ("Alphabet", "Google"),
    "GOOG": ("Alphabet", "Google"),
    "VST": ("Vistra",),
    "IBM": ("International Business Machines", "IBM"),
    "META": ("Meta Platforms", "Facebook", "Instagram"),
    "AAPL": ("Apple",),
    "MSFT": ("Microsoft",),
    "AMZN": ("Amazon",),
    "NVDA": ("Nvidia",),
}
EVENTS = (
    ("earnings/guidance", r"earnings|guidance|forecast|outlook|\beps\b|revenue|profit", 1.0),
    ("regulatory/legal", r"regulat|lawsuit|investigation|court|approval|fine\b", 1.0),
    ("M&A", r"acquir|acquisition|merger|takeover", 1.0),
    ("financing/buyback", r"buyback|repurchase|debt|financing|dividend|share offering", 0.6),
    ("operations/product", r"launch|product|factory|plant|contract|recall|layoff", 0.6),
    ("analyst opinion", r"analyst|upgrade|downgrade|price target", 0.6),
    ("sector/macro", r"sector|economy|inflation|interest rate|federal reserve", 0.6),
    (
        "price recap",
        r"stock (?:rises|falls|gains|drops)|shares (?:rise|fall)|top \d+|best stocks",
        0.2,
    ),
)


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def tokens(value):
    return re.findall(r"[a-z0-9]+", value.casefold())


def context(ticker, name="", extra=()):
    cleaned = re.sub(
        r"\b(?:incorporated|corporation|corp|inc|ltd|plc|class [ab])\b\.?", "", name, flags=re.I
    ).strip(" .,()-")
    names = [*ALIASES.get(ticker, ()), *extra]
    if len(cleaned) >= 3:
        names.append(cleaned)
    return {
        "ticker": ticker,
        "name": name[:160],
        "aliases": sorted(set(names)),
        "version": ALIASES_VERSION,
    }


def mentions(value, company):
    ticker = company["ticker"]
    if re.search(
        r"\$" + re.escape(ticker) + r"\b|\b(?:NYSE|NASDAQ)\s*:\s*" + re.escape(ticker) + r"\b",
        value,
        re.I,
    ):
        return True
    if len(ticker) >= 3 and ticker not in {"THE", "FOR", "ALL", "ARE", "NOW", "NEW", "HAS"}:
        if re.search(r"(?<![A-Za-z])" + re.escape(ticker) + r"(?![A-Za-z])", value):
            return True
    return any(
        re.search(r"(?<!\w)" + re.escape(alias) + r"(?!\w)", value, re.I)
        for alias in company["aliases"]
    )


def relevance(headline, snippet, company):
    if mentions(headline + " " + snippet, company):
        return "direct", "Company identified in supplied text"
    if re.search(
        r"\b(?:sector|industry|economy|inflation|federal reserve)\b", headline + " " + snippet, re.I
    ):
        return "contextual", "Sector or macro context; company not identified"
    if not headline.strip():
        return "unknown", "No usable supplied text"
    # A tag alone is not company evidence. Known other issuers can be rejected.
    if any(
        mentions(headline + " " + snippet, context(t)) for t in ALIASES if t != company["ticker"]
    ):
        return "unrelated", "Supplied text identifies another company"
    return "unknown", "Company relevance is not established by supplied text"


def event_type(value):
    if re.search(
        r"\b(?:top \d+|best stocks|why .{0,50}(?:stock|shares)|stock (?:rises|falls|gains|drops)|shares (?:rise|fall))\b",
        value,
        re.I,
    ) and not re.search(
        r"\b(?:guidance|beat\w* (?:estimates|expectations)|miss\w* (?:estimates|expectations)|regulatory approval|recall|lawsuit)\b",
        value,
        re.I,
    ):
        return "price recap", 0.2
    for kind, pattern, importance in EVENTS:
        if re.search(pattern, value, re.I):
            return kind, importance
    return "other", 0.6


def direction(bias):
    return (
        "unknown"
        if bias is None
        else "positive"
        if bias > 0.15
        else "negative"
        if bias < -0.15
        else "neutral"
    )


def label(bias):
    return {
        "unknown": "No clear signal",
        "positive": "Bullish",
        "negative": "Bearish",
        "neutral": "Neutral",
    }[direction(bias)]


def _signature(row):
    headline = row["headline"]
    normalized = re.sub(
        r"\s+(?:- |\| )?(?:by )?(?:reuters|yahoo finance|benzinga)\s*$", "", headline.casefold()
    )
    words = set(tokens(normalized))
    full_text = headline + " " + row.get("snippet", "")
    figures = re.findall(
        r"[+-]?\d+(?:[.,]\d+)*(?:%|\s*(?:billion|million|thousand|[bmk]\b))?", full_text.casefold()
    )
    periods = re.findall(
        r"\b(?:first|second|third|fourth|q[1-4]|january|february|march|april|may|june|july|august|september|october|november|december|monday|tuesday|wednesday|thursday|friday)\b",
        full_text.casefold(),
    )
    facts = tuple(sorted(set(figures + periods)))
    polarity = tuple(
        sorted(
            set(
                re.findall(
                    r"\b(?:not|no|never|raise\w*|cut\w*|lower\w*|beat\w*|miss\w*|upgrade\w*|downgrade\w*)\b",
                    full_text.casefold(),
                )
            )
        )
    )
    return normalized, words, facts, polarity


def representative_key(row):
    return (
        row["analysis"]["relevance"] == "direct",
        {"article": 3, "snippet": 2, "headline_only": 1, "legacy_extract": 0}.get(
            row["input_quality"], 0
        ),
        len(row["snippet"]),
        row["observed_at"],
        row["id"],
    )


def group_events(rows, ticker, now):
    """Conservative groups; differing numbers/directions never near-merge."""
    groups = []
    signatures = {}
    for row in sorted(rows, key=lambda r: (r.get("first_publication", r["published_at"]), r["id"])):
        published = row.get("first_publication", row["published_at"])
        if not 0 <= now - published <= WINDOW:
            continue
        sig = _signature(row)
        match = None
        for group in groups:
            first = group["members"][0]
            other = signatures[group["id"]]
            same_article = row["article_id"] == first["article_id"]
            compatible = (not sig[2] or not other[2] or sig[2] == other[2]) and (
                not sig[3] or not other[3] or sig[3] == other[3]
            )
            exact = sig[0] == other[0] and compatible
            similar = (
                sig[2:] == other[2:]
                and len(sig[1] & other[1]) >= 5
                and len(sig[1] & other[1]) / max(1, len(sig[1] | other[1])) >= 0.85
            )
            if same_article or exact or similar:
                match = group
                break
        if match is None:
            match = {
                "id": digest(ticker + ":" + str(row["id"])),
                "members": [],
                "published_at": published,
            }
            groups.append(match)
            signatures[match["id"]] = sig
        match["members"].append(row)
    for group in groups:
        candidates = []
        for row in group["members"]:
            if row["input_quality"] == "article":
                prior = _signature(row)
                # A cached article must not outrank a subsequently corrected
                # headline or newly reported figures from the same story.
                newer = [
                    m
                    for m in group["members"]
                    if m["article_id"] == row["article_id"]
                    and m["provider"] != row["provider"]
                    and m["observed_at"] > row["observed_at"]
                ]
                if any(
                    _signature(m)[0] != prior[0] or set(_signature(m)[2]) - set(prior[2])
                    for m in newer
                ):
                    continue
            candidates.append(row)
        group["representative"] = max(candidates or group["members"], key=representative_key)
        views = {direction(r["analysis"].get("bias")) for r in group["members"]} - {"unknown"}
        group["conflict"] = "positive" in views and "negative" in views
    return groups


def second_view(groups):
    states, matched, unmatched, different_context, available = [], 0, 0, 0, 0
    for group in groups:
        local = direction(group["representative"]["analysis"].get("bias"))
        vendors = [
            v
            for member in group["members"]
            for v in member.get("vendor_scores", [])
            if v.get("direction") in ("positive", "negative", "neutral")
        ]
        available += len(vendors)
        if (
            local == "unknown"
            or not vendors
            or group["representative"]["analysis"]["relevance"] != "direct"
        ):
            unmatched += 1
            continue
        matched += 1
        different_context += any(v.get("basis") != "same_supplied_text" for v in vendors)
        for vendor in vendors:
            remote = vendor["direction"]
            states.append(
                "agrees"
                if remote == local
                else "opposite"
                if {local, remote} == {"positive", "negative"}
                else "neutral_difference"
            )
    if not states:
        state = "no matched stories" if available else "unavailable"
    elif len(set(states)) > 1:
        state = "mixed"
    else:
        state = "agrees on direction" if states[0] == "agrees" else "differs"
    return {
        "state": state,
        "matched": matched,
        "unmatched": unmatched,
        "different_context": different_context,
        "available_assessments": available,
        "comparisons": dict((s, states.count(s)) for s in set(states)),
    }


def event_weight(group, now):
    analysis = group["representative"]["analysis"]
    return (
        {"direct": 1, "contextual": 0.25}.get(analysis["relevance"], 0)
        * analysis["importance"]
        * 2 ** (-(now - group["published_at"]) / 86400)
    )


def aggregate(groups, ticker, now, limit=20):
    eligible, unknown = [], 0
    for group in groups:
        row = group["representative"]
        analysis = row["analysis"]
        weight = event_weight(group, now)
        if analysis.get("bias") is None or not weight:
            unknown += 1
        eligible.append((weight, group))
    eligible.sort(key=lambda entry: (entry[0], entry[1]["published_at"]), reverse=True)
    selected = [g for _, g in eligible[:limit]]
    weights = [
        (w, g["representative"]["analysis"]["bias"])
        for w, g in eligible[:limit]
        if w and g["representative"]["analysis"].get("bias") is not None
    ]
    total = sum(w for w, _ in weights)
    bias = round(sum(w * b for w, b in weights) / total, 3) if total else None
    signs = {direction(b) for w, b in weights if w}
    return {
        "ticker": ticker,
        "as_of": now,
        "bias": bias,
        "label": label(bias),
        "event_count": len(selected),
        "unknown_count": unknown,
        "mixed": ("positive" in signs and "negative" in signs)
        or any(g["conflict"] or g["representative"]["analysis"].get("mixed") for g in selected),
        "contextual_only": bool(weights)
        and not any(g["representative"]["analysis"]["relevance"] == "direct" for g in selected),
        "window_seconds": WINDOW,
        "method": VERSION,
        "truncated": len(groups) > limit,
        "second_view": second_view(selected),
        "evidence": [evidence(g) for g in selected[:3]],
    }


def evidence(group):
    row = group["representative"]
    return {
        "event_id": group["id"],
        "revision_id": row["id"],
        "ticker": row["ticker"],
        "provider": row["provider"],
        "publisher": row["publisher"],
        "url": row["url"],
        "headline": row["headline"][:200],
        "snippet": row["snippet"][:280],
        "published_at": group["published_at"],
        "source_published_at": row["published_at"],
        "known_at": max(row["observed_at"], row["associated_at"], row["bound_at"]),
        "input_hash": row["input_hash"],
        "input_quality": row["input_quality"],
        "analysis": row["analysis"],
        "copies": len(group["members"]),
        "vendor_scores": [v for m in group["members"] for v in m.get("vendor_scores", [])][:2],
    }


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)
