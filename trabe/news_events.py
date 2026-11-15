"""Conservative presentation-only facts. Never used by financial signal rules."""

import hashlib
import json
import re
from datetime import UTC, datetime

VERSION = "events-v2"
CONTEXT = "relationships-2026-11-14"
ALIASES = {
    "AMD": ("AMD", "Advanced Micro Devices"),
    "AMZN": ("AMZN", "Amazon", "Amazon Web Services", "AWS"),
    "GOOGL": ("GOOGL", "Alphabet", "Google"),
    "NVDA": ("NVDA", "NVIDIA"),
    "VST": ("VST", "Vistra"),
}
# Relationships are explicit, one hop and dated. Names never imply current office.
RELATIONSHIPS = (
    ("NVDA", "CUDA", "product", "2007-06-01", None, "https://developer.nvidia.com/cuda-zone"),
    (
        "AMD",
        "ROCm",
        "product",
        "2016-04-01",
        None,
        "https://www.amd.com/en/products/software/rocm.html",
    ),
    (
        "AMD",
        "Instinct",
        "product",
        "2016-12-01",
        None,
        "https://www.amd.com/en/products/accelerators/instinct.html",
    ),
    (
        "NVDA",
        "Jensen Huang",
        "executive",
        "1993-04-01",
        "2026-11-12",
        "https://nvidianews.nvidia.com/news/nvidia-announces-ceo-transition",
    ),
    (
        "NVDA",
        "Colette Kress",
        "executive",
        "2026-11-12",
        None,
        "https://nvidianews.nvidia.com/news/nvidia-announces-ceo-transition",
    ),
)


def encoded(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def words(value):
    return " ".join(re.findall(r"[\w$%]+", value.lower()))


def contains(value, name):
    return bool(
        re.search(
            r"(?<!\w)" + re.escape(name) + r"(?!\w)",
            value,
            0 if name.isupper() and len(name) <= 3 else re.I,
        )
    )


def sentences(item):
    # Keep whole source sentences and their stable IDs; never truncate a clause.
    result = [{"id": "h", "text": item["headline"]}]
    for index, sentence in enumerate(re.split(r"(?<=[.!?])\s+", item.get("snippet", "")[:4096])):
        if sentence.strip() and sentence.strip() != item["headline"]:
            result.append({"id": f"s{index}", "text": sentence.strip()})
    return result[:120]


def safe_projection(analysis):
    """A mentioned issuer is not automatically the subject of another clause."""
    kind = analysis["type"]
    if kind not in {"results", "leadership"}:
        return analysis
    names = list(ALIASES.get(analysis["ticker"], (analysis["ticker"],)))
    names += [r["entity"] for r in analysis.get("relationships", []) if r["kind"] == "executive"]
    clauses = re.split(r"[,;:]|\b(?:and|whereas|while)\b", analysis["object"], flags=re.I)
    for clause in clauses:
        if not any(contains(clause, name) for name in names):
            continue
        if (
            kind == "leadership"
            and re.search(r"\b(CEO|chief executive officer)\b", clause, re.I)
            and re.search(
                r"\b(announces?|announced|confirms?|confirmed|appoints?|appointed|resigns?|resigned|departs?|retires?|steps down|replaces?|names)\b",
                clause,
                re.I,
            )
        ):
            return analysis
        if kind == "results" and (
            re.search(
                r"\b(raises?|cuts?|lowers?|withdraws?|raised|cut|lowered)\b.*\bguidance\b",
                clause,
                re.I,
            )
            or re.search(r"\breports?\b.*\b(results|revenue|earnings|EPS)\b", clause, re.I)
        ):
            return analysis
    return analysis | {
        "rules": VERSION,
        "type": "unknown",
        "materiality": "low",
        "direction": "unknown",
        "actor": None,
        "action": "unresolved attribution",
        "push_eligible": False,
        "uncertainty": "Issuer/action attribution unresolved; price impact unknown.",
    }


def analyze(item, ticker):
    spans = sentences(item)
    headline = item["headline"]
    day = datetime.fromtimestamp(item["published_at"], UTC).date().isoformat()
    aliases = ALIASES.get(ticker, (ticker,))
    direct = [s for s in spans if any(contains(s["text"], a) for a in aliases)]
    relationships = [
        {"entity": name, "kind": kind, "since": start, "until": end, "source": source}
        for company, name, kind, start, end, source in RELATIONSHIPS
        if company == ticker
        and start <= day
        and (end is None or day <= end)
        and any(contains(s["text"], name) for s in spans)
    ]
    related = bool(direct or relationships)
    supported = direct or [
        s for s in spans if any(contains(s["text"], r["entity"]) for r in relationships)
    ]
    selected = supported[0]["text"] if supported else ""
    lower = selected.lower()
    uncertain = bool(
        re.search(
            r"\b(could|may|might|rumou?r|denies|denied|not|no plans|considering|proposed|reportedly|plans?|expects?|expected|will|would|should|set to)\b|\?",
            lower,
        )
    )
    recap = bool(
        re.search(
            r"\b(preview|recap|opinion|what to expect|why .* could|to report|schedules?|sets date)\b",
            headline,
            re.I,
        )
    )
    kind, tier, action = "unknown", "low", "unresolved"
    if related and not uncertain and not recap:
        if re.search(r"\b(CEO|chief executive officer)\b", selected, re.I) and re.search(
            r"\b(announces?|announced|confirms?|confirmed|appoints?|appointed|resigns?|resigned|departs?|retires?|steps down|replaces?|names)\b",
            selected,
            re.I,
        ):
            kind, tier, action = "leadership", "high", "CEO transition"
        elif re.search(r"\b(orders?|bans?|blocks?|fines?)\b", lower) and re.search(
            r"\b(regulator|court|commission|FTC|DOJ)\b", selected, re.I
        ):
            kind, tier, action = "regulatory", "high", "binding action"
        elif re.search(
            r"\b(raises?|cuts?|lowers?|withdraws?|raised|cut|lowered)\b.*\bguidance\b", lower
        ):
            kind, tier, action = "results", "high", "guidance change"
        elif re.search(r"\breports?\b.*\b(results|revenue|earnings|EPS)\b", selected, re.I):
            kind, tier, action = "results", "high", "reported results"
        elif re.search(r"\b(customer|contract|partnership)\b", lower) and re.search(
            r"\b(selected|selects|chooses|chose|wins|won|signs|signed|announces|announced)\b", lower
        ):
            kind, tier, action = "customer", "medium", "attributed customer claim"
        elif re.search(r"\b(CUDA|ROCm|Instinct|porting|ported|competes?)\b", selected, re.I):
            kind, tier, action = "competition", "medium", "attributed technology claim"
    if recap:
        kind = "preview" if "preview" in headline.lower() else "recap/opinion"
    mentioned_issuers = [
        s for s, names in ALIASES.items() if any(contains(selected, n) for n in names)
    ]
    ambiguous_actor = len(mentioned_issuers) > 1
    if ambiguous_actor and tier == "high":
        kind, tier, action = "unknown", "low", "unresolved multi-company attribution"
    if re.search(
        r"\b(sponsored|giveaway|guaranteed returns|free stock picks|click here)\b", headline, re.I
    ):
        kind, tier, action = "recap/opinion", "low", "promotion"
    direction = "unknown"
    if tier == "high" and action == "guidance change":
        positive = bool(re.search(r"\braise[sd]?\b", lower))
        negative = bool(re.search(r"\b(cuts?|lower[sd]?|withdraws?)\b", lower))
        direction = "mixed" if positive and negative else "positive" if positive else "negative"
    elif kind == "regulatory":
        direction = "negative"
    elif (
        related
        and not uncertain
        and re.search(r"\b(no material change|guidance remains unchanged)\b", lower)
    ):
        direction = "neutral"
    # Source wording is the fact; price impact is never inferred from importance.
    candidates = sorted(supported, key=lambda s: (len(s["text"]) > 280, s["id"] != "h"))
    facts = [s for s in candidates if len(s["text"]) <= 280][:2]
    if not facts and len(headline) <= 512:
        facts = [{"id": "h", "text": headline}]
    attribution = (
        "company statement"
        if item.get("role") == "issuer"
        else "opinion"
        if kind == "recap/opinion"
        else "reporting"
    )
    return safe_projection(
        {
            "rules": VERSION,
            "context": CONTEXT,
            "ticker": ticker,
            "issuer": ticker if related else None,
            "actor": ticker if direct and not ambiguous_actor else None,
            "action": action,
            "object": selected,
            "type": kind,
            "materiality": tier,
            "direction": direction,
            "direct": bool(direct),
            "relationships": relationships,
            "status": "uncertain/proposed" if uncertain else "announced/reported",
            "event_date": day,
            "facts": facts,
            "attribution": attribution,
            "quality": item.get("input_quality", "headline_only"),
            "uncertainty": "Price impact unknown."
            if direction == "unknown"
            else "Rule interpretation; impact unvalidated.",
            "push_eligible": bool(
                direct and tier == "high" and not uncertain and not ambiguous_actor and facts
            ),
        }
    )


def facts_hash(analysis):
    # Publisher, punctuation and receipt/publication refresh are not new facts.
    return digest(
        encoded(
            {k: analysis[k] for k in ("issuer", "action", "type", "direction", "status")}
            | {
                "object": words(analysis["object"]),
                "facts": [words(f["text"]) for f in analysis["facts"]],
            }
        )
    )
