"""Pure, versioned daily paper classifier. Engineering rules, not a validated strategy."""

from datetime import date, datetime
from decimal import ROUND_FLOOR, Decimal, InvalidOperation, localcontext

from trabe.calendar import NY
from trabe.earnings_results import comparison
from trabe.earnings_results import number as earnings_number
from trabe.market_context import number
from trabe.outlook import digest

RULES = "paper-rules-v1"
PARAMETERS = {
    "rule_version": RULES,
    "initial_cash_usd": "2000.00",
    "fee_eur": "1.00",
    "targets_usd": {"small": "200", "medium": "400", "big": "600"},
    "news_age_seconds": 72 * 3600,
    "news_refresh_seconds": 10800,
    "news_limit": 5,
    "minimum_stories": 2,
    "news_band": "0.2",
    "news_weight": 2,
    "earnings_weight": 1,
    "insider_weight": 1,
    "market_weight": 1,
    "fundamental_refresh_seconds": 86400,
    "event_age_days": 30,
    "decision_before_open_seconds": 900,
    "decision_grace_seconds": 60,
    "preparation_before_open_seconds": 3600,
    "open_ready_seconds": 300,
    "fill_deadline_after_close_seconds": 1800,
    "fx_max_days": 7,
    "fx_policy": "latest valid ECB observation received by execution",
    "quantity": "whole shares",
    "slippage": "0",
    "precision": 28,
    "money_rounding": "ROUND_HALF_UP",
    "ranking": "score descending, canonical ticker",
    "missing_required": "hold",
    "missing_optional_buy_cap": "small",
    "same_open_sale_proceeds": "next session",
    "corporate_actions": "block known; coverage unverified",
}


def fresh(stamp, now, age):
    return (
        isinstance(stamp, (int, float)) and not isinstance(stamp, bool) and 0 <= now - stamp <= age
    )


def whole(value):
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 10**12:
        raise ValueError("Paper shares must be nonnegative whole integers.")
    return value


def comparable_metric(metric, event, now):
    """Require proof for BOTH values. Existing unqualified calendar pairs stay neutral."""
    actual, estimate = metric.get("actual_provenance"), metric.get("estimate_provenance")
    if (
        not isinstance(actual, dict)
        or not isinstance(estimate, dict)
        or metric.get("withdrawn")
        or metric.get("source") == "alphavantage"
    ):
        return None
    for key in ("period", "basis", "currency"):
        if not actual.get(key) or actual[key] != estimate.get(key):
            return None
    if actual["period"] != event.get("period") or actual["currency"] != "USD":
        return None
    for fact in (actual, estimate):
        if (
            not fact.get("source")
            or fact["source"] == "alphavantage"
            or not fact.get("source_url")
            or not fresh(fact.get("known_at"), now, 30 * 86400)
        ):
            return None
    a, e = earnings_number(metric.get("actual")), earnings_number(metric.get("estimate"))
    return comparison(a, e)[0] if a is not None and e is not None else None


def classify(inputs, portfolio, session, parameters=None):
    parameters = parameters or PARAMETERS
    if parameters.get("rule_version") != RULES:
        raise ValueError("Unsupported paper classifier configuration.")
    now = inputs["as_of"]
    ticker = inputs["ticker"]
    held = whole(portfolio.get(ticker, {}).get("quantity", 0))
    gaps, reasons, optional = [], [], []
    components = dict.fromkeys(("N", "E", "I", "M"), 0)
    reference = inputs.get("reference")
    try:
        if (
            not reference
            or reference.get("session_date") != inputs["previous_session"]
            or reference.get("currency") != "USD"
            or reference.get("symbol") != ticker
            or not reference.get("source_url")
            or reference.get("received_at", now + 1) > now
            or (reference.get("bar_time") is not None and reference["bar_time"] >= session.opens)
            or reference.get("source") != "yahoo-chart"
            or reference.get("method")
            not in {"regular-minute-endpoint-v1", "validated-previous-close-v1"}
        ):
            raise ValueError("No previous-session reference.")
        price = number(reference["price"])
    except (ValueError, KeyError, TypeError):
        price = None
        gaps.append("reference_missing")
    if (
        inputs.get("invalid")
        or inputs.get("corporate_actions")
        or (reference and reference.get("actions"))
    ):
        gaps.append("action_or_identity_conflict")
    sources = inputs.get("news_sources", [])
    if not any(
        fresh(s.get("succeeded_at"), now, parameters["news_refresh_seconds"]) for s in sources
    ) or any(s.get("error") for s in sources):
        gaps.append("news_refresh_unavailable")
    news, seen = [], set()
    for item in inputs.get("news", []):
        key = item.get("event_id") or item.get("group_id") or item.get("id")
        if not key or key in seen:
            continue
        if (
            not fresh(item.get("published_at"), now, parameters["news_age_seconds"])
            or not fresh(item.get("known_at"), now, parameters["news_age_seconds"])
            or not fresh(item.get("source_published_at"), now, parameters["news_age_seconds"])
            or item.get("input_quality") not in {"article", "snippet"}
            or not item.get("snippet", "").strip()
            or item.get("analysis", {}).get("relevance") != "direct"
        ):
            continue
        try:
            raw_bias = item.get("analysis", {}).get("bias")
            if isinstance(raw_bias, bool):
                raise ValueError("Invalid bias.")
            bias = Decimal(str(raw_bias))
            if not bias.is_finite() or not -1 <= bias <= 1:
                raise ValueError("Invalid bias.")
        except (ValueError, InvalidOperation):
            continue
        if item.get("conflict") or item.get("analysis", {}).get("mixed"):
            bias = Decimal(0)
        seen.add(key)
        news.append((item["published_at"], str(key), bias))
    news.sort(key=lambda row: (-row[0], row[1]))
    news = news[: parameters["news_limit"]]
    if len(news) < parameters["minimum_stories"]:
        gaps.append("news_insufficient")
    elif not gaps:
        average = sum(n[2] for n in news) / len(news)
        band = Decimal(parameters["news_band"])
        components["N"] = (
            parameters["news_weight"]
            if average >= band
            else -parameters["news_weight"]
            if average <= -band
            else 0
        )
    for kind in ("earnings", "insiders"):
        statuses = [s for s in inputs.get("fundamental_sources", []) if s.get("kind") == kind]
        if (
            not statuses
            or any(s.get("error") for s in statuses)
            or not any(
                fresh(s.get("succeeded_at"), now, parameters["fundamental_refresh_seconds"])
                for s in statuses
            )
        ):
            optional.append(kind + "_coverage_unavailable")
    today = datetime.fromtimestamp(now, NY).date()
    earnings_soon = False
    reported = []
    for row in inputs.get("earnings", []):
        fact = row.get("facts", {})
        if not fresh(row.get("known_at"), now, 120 * 86400):
            continue
        try:
            day = date.fromisoformat(fact["date"])
        except (ValueError, KeyError, TypeError):
            continue
        if fact["date"] in (session.day, inputs.get("next_session")):
            earnings_soon = True
        if (
            fact.get("reported")
            and 0 <= (today - day).days <= parameters["event_age_days"]
            and not fact.get("superseded_by")
            and not fact.get("withdrawn_results")
            and not fact.get("source_conflicts")
            and row.get("source") != "alphavantage"
        ):
            reported.append((day, row.get("key", ""), fact))
    if reported:
        fact = max(reported, key=lambda r: (r[0], r[1]))[2]
        verdicts = [comparable_metric(v, fact, now) for v in fact.get("comparisons", {}).values()]
        if not any(v is not None for v in verdicts):
            optional.append("earnings_comparability_unavailable")
        if "HIT" in verdicts and "MISS" not in verdicts:
            components["E"] = parameters["earnings_weight"]
        elif "MISS" in verdicts and "HIT" not in verdicts:
            components["E"] = -parameters["earnings_weight"]
    insiders_seen = set()
    for row in inputs.get("insiders", []):
        fact = row.get("facts", {})
        try:
            day = date.fromisoformat(fact["transaction_date"])
            identity = fact.get("accession") or fact.get("filing_id")
            if (
                identity
                and identity not in insiders_seen
                and fact.get("eligibility_errors") == []
                and fact.get("transaction_code") == "P"
                and fact.get("derivative_status") == "non-derivative"
                and not fact.get("amendment")
                and fact.get("form_type", "4") == "4"
                and not fact.get("withdrawn")
                and not fact.get("conflict")
                and 0 <= (today - day).days <= parameters["event_age_days"]
                and fresh(row.get("known_at"), now, 120 * 86400)
                and fresh(fact.get("published_at"), now, 120 * 86400)
                and number(fact["value_usd"]) >= Decimal(inputs["insider_min_value_usd"])
            ):
                components["I"] = parameters["insider_weight"]
                insiders_seen.add(identity)
        except (ValueError, KeyError, TypeError):
            continue
    outlook = inputs.get("market_outlook")
    if (
        outlook
        and outlook.get("session_date") == session.day
        and outlook.get("as_of", now + 1) <= now
        and not outlook.get("gaps")
        and outlook.get("forecast", {}).get("session") in {"bullish", "bearish", "mixed"}
    ):
        components["M"] = {
            "bullish": parameters["market_weight"],
            "bearish": -parameters["market_weight"],
            "mixed": 0,
        }[outlook["forecast"]["session"]]
    else:
        optional.append("market_coverage_unavailable")
    for component, kind in (("E", "earnings"), ("I", "insiders")):
        if kind + "_coverage_unavailable" in optional:
            components[component] = 0
    score = sum(components.values())
    action, tier, target = "hold", None, None
    if not gaps:
        if score <= -2:
            target = Decimal(0)
            action = "sell" if held else "hold"
            reasons.append("negative_score")
        elif score >= 2:
            tier = "small" if score == 2 else "medium" if score == 3 else "big"
            target = Decimal(parameters["targets_usd"][tier])
            with localcontext() as ctx:
                ctx.prec = parameters["precision"]
                quantity = int((target / price).to_integral_value(rounding=ROUND_FLOOR))
            action = "buy" if quantity > held else "sell" if quantity < held else "hold"
            if action == "buy" and optional:
                tier = "small"
                target = Decimal(parameters["targets_usd"][tier])
                quantity = int(target / price)
                action = "buy" if quantity > held else "hold"
                reasons.append("optional_coverage_buy_cap")
            if action == "buy":
                if earnings_soon:
                    reasons.append("earnings_soon")
                    action = "hold"
                if not inputs.get("watchlist_member"):
                    reasons.append("removed_from_watchlist")
                    action = "hold"
                if reference.get("instrument_type") != "EQUITY":
                    reasons.append("unsupported_security")
                    action = "hold"
            reasons.append("positive_score_target")
        else:
            reasons.append("neutral_score")
    return {
        "ticker": ticker,
        "action": action,
        "tier": tier,
        "target_usd": str(target) if target is not None else None,
        "score": score,
        "components": components,
        "reasons": reasons,
        "gaps": gaps + optional,
        "reference_price": str(price) if price else None,
        "held_quantity": held,
        "rule_version": RULES,
        "config_sha256": digest(parameters),
        "inputs_sha256": digest(inputs),
        "inputs": inputs,
    }
