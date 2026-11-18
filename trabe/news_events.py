"""Conservative presentation-only facts. Never used by financial signal rules."""

import hashlib
import json
import re
from datetime import UTC, datetime

VERSION = "events-v3"
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


def _sentence_texts(text):
    # A decimal is never a boundary; protect common feed abbreviations as well.
    boundary = r"(?<=[.!?])\s+"
    start = 0
    for match in re.finditer(boundary, text):
        before = text[start : match.start()]
        if re.search(
            r"\b(?:[A-Z]\.){1,3}$|\b(?:Mr|Mrs|Ms|Dr|Inc|Corp|Dec|Nov|Oct|Sept)\.$", before
        ):
            continue
        yield before.strip()
        start = match.end()
    if text[start:].strip():
        yield text[start:].strip()


def sentences(item):
    # Whole source sentences retain original IDs/text, including qualifications.
    result = [{"id": "h", "text": item["headline"]}]
    body = item.get("snippet", "")
    parts = list(_sentence_texts(body[:4096]))
    if (
        len(body) > 4096
        and parts
        and (body[4096] not in " \n\r\t" or not re.search(r"[.!?]$", parts[-1]))
    ):
        parts.pop()  # The input cap must not turn a partial sentence into a fact.
    for index, sentence in enumerate(parts):
        if sentence and sentence != item["headline"]:
            result.append({"id": f"s{index}", "text": sentence})
    return result[:120]


def _clauses(text):
    for sentence in _sentence_texts(text):
        # Keep numeric separators and ticker suffixes (NYSE:VST) intact.
        for clause in re.split(
            r";|,\s+|:\s+|\b(?:and(?!\s+(?:CEO|chief executive))|whereas|while|but)\b",
            sentence,
            flags=re.I,
        ):
            if clause.strip():
                yield clause.strip()


def _positions(text, names):
    return [
        match.start()
        for name in names
        for match in re.finditer(
            r"(?<!\w)" + re.escape(name) + r"(?!\w)",
            text,
            0 if name.isupper() and len(name) <= 3 else re.I,
        )
    ]


def _actor_binding(clause, names, verb, kind):
    for name in names:
        for match in re.finditer(r"(?<!\w)" + re.escape(name) + r"(?!\w)", clause, re.I):
            tail = clause[match.end() : verb.start()]
            if match.end() > verb.start() or len(tail) > 100:
                continue
            # Permit grammatical auxiliaries, not an arbitrary intervening actor.
            filler = r"(?:['’]s|[\s().:-]|has|had|have|is|was|were|been|being|today|just|also|now|already|recently|officially|said|says|that|it|its|not|never|did|didn't|hasn't|wasn't|may|might|could|will|would|should|to|plans|planned|expected|reportedly|denies|denied|considering|proposes|proposed|expects|the|company|Corp|Inc)*"
            if re.fullmatch(filler, tail, re.I):
                return True
            if kind == "leadership" and re.fullmatch(
                r"(?:['’]s)?\s*(?:founder\s+and\s+)?(?:CEO|chief executive officer)\s+(?:[A-Z][\w’'-]*\s+){0,3}(?:(?:has|had|was|is|just|also)\s+)*",
                tail,
            ):
                return True
            if kind == "customer" and re.fullmatch(
                r"\s+says?\s+(?:a|the)\s+customer\s+", tail, re.I
            ):
                return True
    return False


def _affected_binding(clause, positions, verb):
    return any(
        position >= verb.end()
        and re.fullmatch(
            r"(?:\s|[-$.,\d]|a\b|an\b|the\b|against\b|to\b|on\b|with\b|refund\b|settlement\b|million\b|billion\b)*",
            clause[verb.end() : position],
            re.I,
        )
        for position in positions
    )


def _status(clause, predicate):
    # Qualifiers after a completed predicate (e.g. possible fees) cannot negate it.
    prefix = clause[: predicate.end()]
    if re.search(
        r"\b(denies?|denied|not(?! only)|never|no plans|didn't|hasn't|wasn't)\b", prefix, re.I
    ):
        return "denied"
    if "?" in clause or re.search(
        r"\b(could|may|might|rumou?r\w*|consider\w*|propos\w*|reportedly|plans?|planned|expects?|expected|will|would|should|set to|to report)\b",
        prefix,
        re.I,
    ):
        return "proposed"
    return "reported"


def _predicate(clause):
    # Each family requires an event verb; bare dollar amounts/nouns never qualify.
    patterns = (
        (
            "leadership",
            "high",
            "CEO transition",
            r"\b(?:announces?|announced|confirms?|confirmed|appoints?|appointed|resigns?|resigned|departs?|departed|retires?|retired|steps? down|stepped down|replaces?|replaced|names|named)\b",
            r"\b(?:CEO|chief executive officer)\b",
        ),
        (
            "results",
            "high",
            "guidance change",
            r"\b(?:raises?|raised|cuts?|lowers?|lowered|withdraws?|withdrew|withdrawn)\b",
            r"\bguidance\b",
        ),
        (
            "results",
            "high",
            "reported impairment",
            r"\b(?:reports?|reported|posts?|posted|discloses?|disclosed|records?|recorded|recognizes?|recognized)\b",
            r"\bimpairments?\b.*\b(?:assets?|quarter|financial)\b|\b(?:assets?|quarter|financial)\b.*\bimpairments?\b",
        ),
        (
            "results",
            "high",
            "reported results",
            r"\b(?:reports?|reported|posts?|posted|discloses?|disclosed)\b",
            r"\b(?:results|revenue|earnings|EPS|sales|net income)\b",
        ),
        (
            "regulatory",
            "high",
            "binding action",
            r"\b(?:orders?|ordered|bans?|banned|blocks?|blocked|fines?|fined)\b",
            r"\b(?:regulator|court|commission|FTC|DOJ)\b",
        ),
        (
            "regulatory",
            "high",
            "settlement",
            r"\b(?:enters?|entered|reach(?:es)?|reached|agrees?|agreed|approves?|approved)\b",
            r"\bsettlement\b",
        ),
        (
            "regulatory",
            "medium",
            "regulatory compliance",
            r"\b(?:complies|comply|complying|complied)\b",
            r"\b(?:conditions|regulator|regulatory|approval)\b",
        ),
        (
            "regulatory",
            "medium",
            "regulatory approval",
            r"\b(?:approves?|approved|grants?|granted|receives?|received|secures?|secured)\b",
            r"\b(?:approval|regulator|regulatory|commission|FTC|DOJ)\b",
        ),
        (
            "customer",
            "medium",
            "attributed customer claim",
            r"\b(?:selected|selects|chooses|chose|wins|won|signs|signed|announces|announced)\b",
            r"\b(?:customer|contract|partnership)\b",
        ),
        (
            "competition",
            "medium",
            "attributed technology claim",
            r"\b(?:announces?|announced|launches?|launched|introduces?|introduced|supports?|supported|adds?|added|ports?|ported|porting|competes?|competed|optimizes?|optimize|run|running|available|weakens?|weakened)\b",
            r"\b(?:CUDA|ROCm|Instinct|technology|platform|model|GPUs?|chips?|system|Marketplace|Bedrock|compiler|porting|launch)\b",
        ),
    )
    for kind, tier, action, verb, subject in patterns:
        match = re.search(verb, clause, re.I)
        if match and re.search(subject, clause, re.I):
            if (
                kind == "leadership"
                and re.fullmatch(r"announces?|announced|confirms?|confirmed", match[0], re.I)
                and not re.search(
                    r"\b(?:transition|departure|resignation|appointment|successor|replacement|retirement|resigned|appointed|stepped down)\b",
                    clause[match.end() :],
                    re.I,
                )
            ):
                continue
            # "Analysts report Amazon earnings estimates" is not issuer reporting.
            if kind == "results" and re.search(
                r"\b(?:analysts?|estimates?|consensus|forecast)\b", clause[: match.end()], re.I
            ):
                continue
            return kind, tier, action, match
    # Denials/proposals of nominal transitions are still identifiable, low-tier events.
    match = re.search(
        r"\b(?:denies?|denied|considering|proposes?|proposed|replace)\b", clause, re.I
    )
    if match and re.search(
        r"\bCEO\b.*\b(?:departure|transition|rumors?)\b|\breplace\b.*\bCEO\b", clause, re.I
    ):
        return "leadership", "high", "CEO transition", match
    return None


def _candidates(spans, ticker, relationships):
    aliases = ALIASES.get(ticker, (ticker,))
    executives = [r["entity"] for r in relationships if r["kind"] == "executive"]
    products = [r["entity"] for r in relationships if r["kind"] == "product"]
    result = []
    for index, span in enumerate(spans):
        for clause in _clauses(span["text"]):
            predicate = _predicate(clause)
            if not predicate:
                continue
            kind, tier, action, verb = predicate
            names = [*aliases, *(executives if kind == "leadership" else ())]
            positions = _positions(clause, names)
            product = bool(_positions(clause, products))
            if not positions and not product:
                continue
            issuers = [s for s, names in ALIASES.items() if _positions(clause, names)]
            actor = _actor_binding(clause, names, verb, kind)
            affected = kind == "regulatory" and _affected_binding(clause, positions, verb)
            if action == "binding action":
                # A company quoting a court order is not its affected company.
                actor = actor and bool(
                    re.search(r"\b(?:was|is|been|being)\s*$", clause[: verb.start()], re.I)
                )
            # Multiple named issuers within one unsplit clause remain unresolved.
            bound = (actor or affected) and len(issuers) <= 1
            relevance = "direct" if bound else "indirect" if kind == "competition" else "incidental"
            if relevance == "incidental":
                continue
            status = _status(clause, verb)
            if kind == "results" and re.search(
                r"\b(?:expected|estimated|forecast|consensus)\s+(?:\w+\s+){0,2}(?:results|revenue|earnings|EPS|sales)\b|\b(?:results|revenue|earnings|EPS|sales)\s+estimates?\b|\b(?:will|plans? to)\s+report\b",
                clause[verb.end() :],
                re.I,
            ):
                status = "proposed"
            if action == "settlement" and not (actor or re.search(r"\bcourt\b", clause, re.I)):
                tier = "medium"
            direction = "unknown"
            if bound and status == "reported" and action == "guidance change":
                direction = (
                    "positive" if re.fullmatch(r"raises?|raised", verb[0], re.I) else "negative"
                )
            elif bound and status == "reported" and action == "binding action":
                direction = "negative"
            result.append(
                {
                    "span": span,
                    "index": index,
                    "type": kind,
                    "materiality": tier if status == "reported" else "low",
                    "action": action,
                    "event_status": status,
                    "relevance": relevance,
                    "actor": ticker if bound else None,
                    "direction": direction,
                }
            )
    return result


def _news_role(item, selected, ticker):
    headline = item["headline"]
    text = headline + " " + item.get("snippet", "")[:4096]
    if re.search(
        r"\b(?:recap|roundup|weekly|week in review|portfolio review|stock market news for|turbulent week)\b",
        text,
        re.I,
    ):
        return "recap"
    if re.search(
        r"\b(?:opinion|advisor insights|investment strategy|ongoing|back in \d{4})\b", text, re.I
    ):
        return "background"
    if re.search(
        r"\b(?:last quarter|previously|earlier|after .*resignation|after .*departure)\b",
        headline + " " + selected,
        re.I,
    ):
        return "background"
    if selected and not any(contains(headline, name) for name in ALIASES.get(ticker, (ticker,))):
        return "background"
    return "current" if selected else "unknown"


def _direct_commentary(spans, aliases):
    # Relevance is broader than the material-event predicate table. An explicit
    # target business/price statement need not establish a corporate event/actor.
    topic = r"(?:stock|shares|share price|earnings|revenue|CEO|leadership|outlook|guidance|price|forecast|business|growth|weakness|turmoil|resignation|record quarter|dramatic slide)\b"
    verbs = r"\b(?:is|are|was|were|has|had|grew|grows|shed|fell|falls|fallen|sink|sinks|sank|gained|gains|slid|slides|trades|stands|powers|expanded|expands|opened|opens|increased|increases|declined|declines|rallied|rallies)\b"
    for span in spans:
        for clause in _clauses(span["text"]):
            for alias in aliases:
                for match in re.finditer(r"(?<!\w)" + re.escape(alias) + r"(?!\w)", clause, re.I):
                    before, after = clause[: match.start()], clause[match.end() :]
                    if re.match(r"(?:['’]s)?\s+(?:(?:own|new)\s+)?" + topic, after, re.I):
                        return True
                    subject = re.fullmatch(r"\s*(?:Key Takeaways\s*)?", before, re.I)
                    if (
                        subject
                        and re.search(verbs, after, re.I)
                        and not re.match(
                            r"\s+(?:as|with)\b|(?:['’]s)?\s+(?:supplier|customer|competitor|partner|rival|vendor)\b",
                            after,
                            re.I,
                        )
                    ):
                        return True
    return False


def _legacy_projection(analysis):
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
        "rules": analysis.get("rules", "events-v2"),
        "type": "unknown",
        "materiality": "low",
        "direction": "unknown",
        "actor": None,
        "action": "unresolved attribution",
        "push_eligible": False,
        "uncertainty": "Issuer/action attribution unresolved; price impact unknown.",
    }


def safe_projection(analysis):
    """Validate frozen support on reads/flush, without promoting or rewriting it."""
    if analysis.get("rules") != VERSION:
        return _legacy_projection(analysis)
    candidates = _candidates(
        [{"id": "object", "text": analysis["object"]}],
        analysis["ticker"],
        analysis.get("relationships", []),
    )
    matching = [
        c
        for c in candidates
        if c["type"] == analysis["type"]
        and c["action"] == analysis["action"]
        and c["event_status"] == analysis.get("event_status")
        and c["relevance"] == analysis.get("relevance")
        and c["materiality"] == analysis["materiality"]
        and c["actor"] == analysis.get("actor")
        and c["direction"] == analysis["direction"]
    ]
    if analysis["materiality"] in {"high", "medium"} and not matching:
        return analysis | {
            "type": "unknown",
            "materiality": "low",
            "direction": "unknown",
            "actor": None,
            "action": "unresolved attribution",
            "push_eligible": False,
            "uncertainty": "Issuer/action attribution unresolved; price impact unknown.",
        }
    supported = any(
        f.get("text") == analysis["object"] and len(f["text"]) <= 280
        for f in analysis.get("facts", [])
    )
    resolved = matching and not any(
        c["event_status"] == "denied"
        and c["type"] == analysis["type"]
        and c["action"] == analysis["action"]
        for c in candidates
    )
    return analysis | {
        "push_eligible": bool(
            analysis["push_eligible"]
            and resolved
            and supported
            and analysis["direct"]
            and analysis.get("relevance") == "direct"
            and analysis.get("actor") == analysis["ticker"]
            and analysis.get("event_status") == "reported"
            and analysis.get("news_role") == "current"
            and analysis["materiality"] == "high"
        )
    }


def analyze(item, ticker):
    spans = sentences(item)
    headline = item["headline"]
    day = datetime.fromtimestamp(item["published_at"], UTC).date().isoformat()
    aliases = ALIASES.get(ticker, (ticker,))
    mentions = [s for s in spans if any(contains(s["text"], a) for a in aliases)]
    relationships = [
        {"entity": name, "kind": kind, "since": start, "until": end, "source": source}
        for company, name, kind, start, end, source in RELATIONSHIPS
        if company == ticker
        and start <= day
        and (end is None or day <= end)
        and any(contains(s["text"], name) for s in spans)
    ]
    supported = mentions or [
        s for s in spans if any(contains(s["text"], r["entity"]) for r in relationships)
    ]
    candidates = _candidates(spans, ticker, relationships)
    candidates.sort(
        key=lambda c: (
            c["event_status"] != "reported",
            -{"high": 3, "medium": 2, "low": 1}[c["materiality"]],
            c["relevance"] != "direct",
            c["index"],
        )
    )
    selected = candidates[0] if candidates else None
    object_text = (
        selected["span"]["text"] if selected else supported[0]["text"] if supported else ""
    )
    kind, tier, action = (
        (selected["type"], selected["materiality"], selected["action"])
        if selected
        else ("unknown", "low", "unresolved")
    )
    status = selected["event_status"] if selected else "unknown"
    role = _news_role(item, object_text, ticker)
    # Direct relevance includes target-specific commentary; it does not assign an actor.
    incidental = bool(
        re.search(
            r"\b(?:stocks? (?:to|that)\b|stocks? meeting|investment strategy|as a key customer|stocks? with)\b",
            headline + " " + object_text,
            re.I,
        )
    )
    commentary = bool(
        mentions
        and (
            (not incidental and any(contains(headline, a) for a in aliases))
            or _direct_commentary(spans, aliases)
        )
    )
    relevance = (
        selected["relevance"]
        if selected
        else "direct"
        if commentary
        else "incidental"
        if mentions
        else "indirect"
        if relationships
        else "unknown"
    )
    actor = selected["actor"] if selected else None
    direction = selected["direction"] if selected else "unknown"
    conflicting = bool(
        selected
        and any(
            c["type"] == selected["type"]
            and c["action"] == selected["action"]
            and c["event_status"] == "reported"
            for c in candidates
        )
        and (
            any(
                c["type"] == selected["type"]
                and c["action"] == selected["action"]
                and c["event_status"] == "denied"
                for c in candidates
            )
            or {
                c["direction"]
                for c in candidates
                if c["type"] == selected["type"] and c["action"] == selected["action"]
            }
            >= {"positive", "negative"}
        )
    )
    if conflicting:
        status, tier, direction = "unknown", "low", "unknown"
    if not selected and re.search(
        r"\b(?:preview|to report|schedules?|sets date)\b", headline, re.I
    ):
        kind, status = "preview", "proposed"
    elif not selected and re.search(r"\b(?:recap|opinion)\b", headline, re.I):
        kind = "recap/opinion"
    if re.search(
        r"\b(?:sponsored|giveaway|guaranteed returns|free stock picks|click here)\b", headline, re.I
    ):
        kind, tier, action, status = "recap/opinion", "low", "promotion", "unknown"
    if (
        not selected
        and commentary
        and re.search(r"\b(?:no material change|guidance remains unchanged)\b", object_text, re.I)
    ):
        direction = "neutral"
    if selected:
        facts = []
        for candidate in candidates:
            if (
                all(
                    candidate[k] == selected[k]
                    for k in ("type", "action", "event_status", "relevance")
                )
                and len(candidate["span"]["text"]) <= 280
                and candidate["span"] not in facts
            ):
                facts.append(candidate["span"])
        facts = facts[:2]
    else:
        facts = [s for s in supported if len(s["text"]) <= 280][:2]
    supporting_fact = bool(selected and selected["span"] in facts)
    if not facts and len(headline) <= 512:
        facts = [{"id": "h", "text": headline}]
    uncertainty = (
        "Conflicting source statements; no resolved event or price impact inferred."
        if conflicting
        else "Price impact unknown."
        if direction == "unknown"
        else "Rule interpretation; impact unvalidated."
    )
    if selected and not supporting_fact:
        uncertainty = "Supporting event sentence exceeds the display limit; digest only. Price impact unknown."
    return safe_projection(
        {
            "rules": VERSION,
            "context": CONTEXT,
            "ticker": ticker,
            "issuer": ticker if supported else None,
            "actor": actor,
            "action": action,
            "object": object_text,
            "type": kind,
            "materiality": tier,
            "direction": direction,
            "direct": relevance == "direct",
            "relevance": relevance,
            "event_status": status,
            "news_role": role,
            "relationships": relationships,
            "status": "announced/reported" if status == "reported" else "uncertain/proposed",
            "event_date": day,
            "facts": facts,
            "attribution": "company statement"
            if item.get("role") == "issuer"
            else "opinion"
            if kind == "recap/opinion"
            else "reporting",
            "quality": item.get("input_quality", "headline_only"),
            "uncertainty": uncertainty,
            "push_eligible": bool(
                supporting_fact
                and relevance == "direct"
                and actor
                and tier == "high"
                and status == "reported"
                and role == "current"
                and not conflicting
            ),
        }
    )


_INTERIM = re.compile(
    r"\b(?:appointed|named|serve as|step in as)\b.*\b(?:interim|acting) (?:CEO|chief executive)\b",
    re.I,
)
_LEGAL = re.compile(r"\b(?:lawsuit|class.action|settlement)\b", re.I)
_EDITORIAL = re.compile(
    r"^(?:why\b|what(?:’s|'s| is) next\b|prediction:)|"
    r"\b(?:what it means for|columnist argues|make a great point|should you buy)\b",
    re.I,
)
_DIRECT_STATEMENT = re.compile(
    r"\s+(?:is developing|has developed|is building|is launching|"
    r"(?:shares\s+|stock\s+)?(?:just\s+)?(?:dropped|fell|rose|gained)|"
    r"(?:\([^)]{1,45}\)\s+)?board of directors announced)\b",
    re.I,
)
_COMMENTARY_TEASER = re.compile(r"\b(?:make a great point|weigh(?:s|ed)? in)\b", re.I)
_FEED_HEADING = re.compile(r"^Key Takeaways\s*:?\s*(?=[A-Z$\d])")
_BROKEN_DECIMAL = re.compile(r"\d\.\s+\d")
_OPERATING_UPDATE = re.compile(
    r"\s+(?:(?:has|just)\s+)?(?:"
    r"(?:beat|beats|exceeded|exceeds|missed|misses)\s+[^.!?;]{0,90}\b(?:estimates|expectations)\b|"
    r"(?:raised|raises|cut|cuts|lowered|lowers|reaffirmed)\b[^.!?;]{0,90}\b(?:guidance|outlook|forecast)\b)",
    re.I,
)


def _summary_body(spans):
    # Some feeds insert a space into a decimal before splitting the snippet.
    # Reject both pieces. The original evidence and any valid headline stay intact.
    split = {
        s["id"]
        for first, second in zip(spans, spans[1:], strict=False)
        if re.search(r"\d\.$", first["text"]) and re.match(r"\d", second["text"])
        for s in (first, second)
    }
    body = []
    rejected = []
    for span in spans:
        text = _FEED_HEADING.sub("", span["text"])
        ending = text.rstrip("\"'’”)]")
        if (
            span["id"] in split
            or _BROKEN_DECIMAL.search(text)
            or len(text.encode("utf-16-le")) > 760
            or not ending.endswith((".", "!", "?"))
            or ending.endswith("...")
        ):
            rejected.append(text)
        else:
            body.append(span | {"text": text})
    return body, rejected


def _summary_strength(span, aliases, relationships, ticker):
    text = span["text"]
    if any(
        text[: len(alias)].casefold() == alias.casefold()
        and _OPERATING_UPDATE.match(text[len(alias) :])
        for alias in aliases
    ):
        return 5
    actions = _candidates([span], ticker, relationships)
    if any(c["relevance"] == "direct" for c in actions):
        return 4
    if any(
        text[: len(alias)].casefold() == alias.casefold()
        and _DIRECT_STATEMENT.match(text[len(alias) :])
        for alias in aliases
    ):
        return 4
    if actions or _direct_commentary([span], aliases):
        return 3
    return 2


def select_summary(spans, ticker, relationships):
    """Choose whole excerpts for one company from a single saved source packet."""
    aliases = ALIASES.get(ticker, (ticker,))
    headline = spans[0]
    title = headline["text"]
    title_mentions = any(contains(title, a) for a in aliases)
    peers = [
        s for s, names in ALIASES.items() if s != ticker and any(contains(title, a) for a in names)
    ]
    # A related name in someone else's quote/bio does not establish a company item.
    bio = any(
        re.search(r"(?<!\w)" + re.escape(a) + r"\s+(?:co-)?founder\b", s["text"], re.I)
        for a in aliases
        for s in spans
    )
    related_names = [r["entity"] for r in relationships]
    body, rejected = _summary_body(spans[1:])
    relevant = [
        s
        for s in body
        if any(contains(s["text"], a) for a in (*aliases, *related_names))
        and not _COMMENTARY_TEASER.search(s["text"])
    ]
    if _BROKEN_DECIMAL.search(title):
        return None
    if not title_mentions and not relevant:
        if not bio and any(contains(text, a) for text in rejected for a in aliases):
            return {
                "facts": [headline],
                "compact_facts": [headline],
                "display_context": "Related coverage",
            }
        return None
    if not title_mentions and bio and not _direct_commentary(relevant, aliases):
        return None
    lead_clause = re.split(r"\b(?:as|after|following|amid)\b", title, maxsplit=1, flags=re.I)[0]
    secondary = (
        title_mentions
        and not any(contains(lead_clause, a) for a in aliases)
        and bool(
            re.search(
                r"\b(?:shares|stock|bet|fell|rose|traded|gains?|equity (?:markets|indexes))\b",
                lead_clause,
                re.I,
            )
        )
    )
    editorial = bool(
        _EDITORIAL.search(title) or any(_EDITORIAL.search(s["text"]) for s in body[:1])
    )
    round_up = bool(peers and re.search(r"\band More Stocks\b|\bmarket roundup\b", title, re.I))
    if round_up and not any(_direct_commentary([s], aliases) for s in relevant):
        return None

    def strength(s):
        text = s["text"]
        return (
            _summary_strength(s, aliases, relationships, ticker),
            # Concrete amounts, timings and explicit qualifications add information.
            len(
                re.findall(
                    r"\$|\d+(?:%|\s*(?:million|billion))|"
                    r"\b(?:effective|interim|ongoing|alleges?|reportedly|private placement|compared)\b",
                    text,
                    re.I,
                )
            ),
            s["id"] != "h",
        )

    options = [s for s in [headline, *relevant] if (s != headline or title_mentions)]
    legal = [s for s in relevant if _LEGAL.search(title) and _LEGAL.search(s["text"])]
    private = next(
        (
            s
            for s in relevant
            if re.search(r"\bprivate placement\b", s["text"], re.I)
            and re.search(r"\bIPO\b", title, re.I)
        ),
        None,
    )
    interim = next(
        (
            s
            for s in body[:3]
            if title_mentions
            and not peers
            and re.search(r"\b(?:CEO|resignation)\b", title, re.I)
            and _INTERIM.search(s["text"])
            and (
                s in relevant
                or (body[0] in relevant and re.search(r"\bCEO\b", body[0]["text"], re.I))
            )
        ),
        None,
    )
    pair = []
    detail = []
    if legal:
        lead = legal[0]
        if re.search(r"\bongoing\b", lead["text"], re.I) and re.search(
            r"\bsettlement\b", title, re.I
        ):
            pair = [headline]
        else:
            pair = [
                s for s in legal[1:] if re.search(r"\balleges?\b|\ballegedly\b", s["text"], re.I)
            ][:1]
    elif private:
        # The body identifies the transaction. Repeating an incompatible title
        # would leave two contradictory claims on the same card.
        lead = private
    elif interim:
        lead = interim
        detail = [
            s
            for s in relevant
            if s != interim and _summary_strength(s, aliases, relationships, ticker) >= 4
        ][:1]
    else:
        lead = max(options, key=strength)
        if (
            lead == headline
            and len(body) >= 2
            and peers
            and re.match(r"\bOne (?:chipmaker|company)\b", body[0]["text"], re.I)
            and re.match(r"\bIts (?:biggest )?rival\b", body[1]["text"], re.I)
            and any(re.match(re.escape(a) + r"\b", title, re.I) for a in aliases)
            and re.search(r"\b(?:beat|beats|outperformed|outperforms)\b", title, re.I)
        ):
            # Keep the title beside the comparative lead, which has no name.
            pair = [body[0]]
        else:
            detail = [
                s for s in relevant if s != lead and words(s["text"]) not in words(lead["text"])
            ]
            detail = sorted(detail, key=strength, reverse=True)[:1]

    # A dependent excerpt must travel with its lead. Never truncate to fit.
    detailed = [lead, *(pair or detail)]
    compact = [lead, *pair]
    if sum(len(s["text"].encode("utf-16-le")) // 2 + 2 for s in compact) > 450:
        compact = [lead]
    context = ""
    if editorial and not interim and not private:
        context = "Commentary"
    elif secondary or (not title_mentions and all(strength(s)[0] < 4 for s in relevant)):
        context = "Related coverage"
    return {"facts": detailed, "compact_facts": compact, "display_context": context}


def present_event(event, evidence, symbols):
    """Project a frozen revision for display, using its credited source."""
    primary = event["ticker"]
    symbols = set(event["issuers"]).intersection(symbols)
    if not symbols:
        return None
    if event.get("correction") or not evidence.get("headline"):
        if primary in symbols or event.get("correction"):
            return event | {"display_ticker": primary if primary in symbols else min(symbols)}
        return None

    spans = sentences(evidence)
    if not evidence.get("snippet") and evidence.get("sentences"):
        # Storage may retain leading whole sentences after dropping a large extract.
        spans = [spans[0], *(s for s in evidence["sentences"] if s["id"] != "h")]
    for ticker in dict.fromkeys([primary, *sorted(symbols)]):
        if ticker not in symbols:
            continue
        relationships = event.get("relationships", []) if ticker == primary else []
        selected = select_summary(spans, ticker, relationships)
        if selected is not None:
            if ticker != primary:
                selected["display_context"] = selected["display_context"] or "Related coverage"
                selected |= {"event_status": "unknown", "news_role": "unknown", "uncertainty": ""}
            if selected["display_context"]:
                selected |= {"materiality": "low", "direction": "unknown"}
            quality = evidence.get("input_quality") or event["quality"]
            return (
                event
                | selected
                | {
                    "display_ticker": ticker,
                    "display_headline": spans[0]["text"],
                    "quality": quality if len(spans) > 1 else "headline_only",
                    "attribution": "company statement"
                    if evidence.get("role") == "issuer"
                    else "reporting",
                }
            )
    return None


_BRIEFING_RESULTS = re.compile(
    r"(?:has\s+)?(?:"
    r"(?:beats?|exceeded|exceeds|missed|misses)\s+"
    r"(?:(?:first[- ]quarter|second[- ]quarter|third[- ]quarter|fourth[- ]quarter|"
    r"quarterly|Q[1-4]|consensus|Wall Street|analysts?['’]?)\s+){0,4}(?:estimates|expectations)|"
    r"(?:reports?|reported|posts?|posted)\s+"
    r"(?:(?:first[- ]quarter|second[- ]quarter|third[- ]quarter|fourth[- ]quarter|"
    r"quarterly|annual|fiscal|Q[1-4]|record|adjusted|unaudited)\s+){0,5}"
    r"(?:results|revenue|earnings|sales|net income|EPS)"
    r"(?:\s+of\s+[\d$€£,.%]+(?:\s+(?:million|billion|trillion))?)?)",
    re.I,
)
_BRIEFING_CHANGE = re.compile(
    r"(?:has\s+)?(?:raises?|raised|cuts?|lowered|lowers?|increased|increases|reduced|reduces|"
    r"withdrew|withdraws)\s+"
    r"(?P<scope>(?:(?:its|the|next|current|this|upcoming|fiscal|financial|year(?:['’]s)?|"
    r"quarter(?:['’]s)?|full[- ]year|(?:first|second|third|fourth)[- ]quarter|Q[1-4]|"
    r"FY\d{2,4}|20\d\d|net|revenue|growth|earnings|EPS|profit|income|margin|sales|"
    r"adjusted|operating|free|cash|flow|capital|expenditure)\s+){1,12})"
    r"(?:guidance|outlook|forecast)(?=$|[.!?,]|\s+(?:to|from|by)\s+(?:about\s+)?[\d$€£])",
    re.I,
)
_BRIEFING_PERIOD = re.compile(
    r"\b(?:full[- ]year|(?:next|current|this|upcoming)(?:\s+(?:fiscal|financial))?\s+"
    r"(?:year|quarter)|(?:first|second|third|fourth)[- ]quarter|Q[1-4]|FY\d{2,4})\b",
    re.I,
)
_BRIEFING_UNCERTAIN = re.compile(
    r"\b(?:if|unless|may|might|could|would|should|hypothetical|reportedly|"
    r"allegedly|rumou?r\w*|denies|denied|not|never|previously|last year|years ago|"
    r"expect(?:s|ed)? to|analysts?(?:['’]s)? (?:forecast|projection))\b|\?",
    re.I,
)
_BRIEFING_CONTEXT = re.compile(
    r"\b(?:recap|round[- ]?up|flashback|year in review|look(?:ing)? back|previously|years ago|"
    r"earnings preview|what to expect|before earnings|upcoming earnings|what if)\b",
    re.I,
)


def briefing_results_fact(event):
    """Find explicitly reported results and a guidance change with one issuer subject."""
    if (
        event.get("news_role") != "current"
        or event.get("relevance") != "direct"
        or event.get("event_status") not in {"reported", "unknown"}
        or event.get("correction")
        or event.get("display_context")
        or _BRIEFING_CONTEXT.search(event.get("display_headline", ""))
        or event["uncertainty"].startswith("Conflicting source statements")
    ):
        return None
    ticker = event["ticker"]
    aliases = ALIASES.get(ticker, (ticker,))
    for fact in event["facts"]:
        text = fact["text"]
        if fact["id"] == "h" or _BRIEFING_UNCERTAIN.search(text) or _BRIEFING_CONTEXT.search(text):
            continue
        parts = re.split(r",?\s+(?:and|but)\s+", text, maxsplit=1, flags=re.I)
        if len(parts) != 2:
            continue
        result, guidance = parts
        change = _BRIEFING_CHANGE.match(guidance)
        if not change or not _BRIEFING_PERIOD.search(change["scope"]):
            continue
        for name in aliases:
            if name in {"AWS", "Amazon Web Services"}:
                continue
            # A ticker has to match its capitalization. Company names can vary.
            prefix = result[: len(name)]
            named_issuer = (
                prefix == ticker if name == ticker else prefix.casefold() == name.casefold()
            )
            suffix = result[len(name) :]
            if (
                named_issuer
                and suffix.startswith(" ")
                and _BRIEFING_RESULTS.fullmatch(suffix.strip())
            ):
                return fact
    return None


def facts_hash(analysis):
    # Publisher, punctuation and receipt/publication refresh are not new facts.
    return digest(
        encoded(
            {k: analysis[k] for k in ("issuer", "action", "type", "direction", "status")}
            | {
                "object": words(analysis["object"]),
                "facts": [words(f["text"]) for f in analysis["facts"]],
                "issuers": sorted(analysis.get("issuers", [analysis["issuer"]])),
            }
        )
    )
