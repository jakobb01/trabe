"""Bounded quarterly EPS history and comparable, explicitly sourced results."""

import hashlib
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from trabe.api import ApiError
from trabe.calendar import NY, USCalendar
from trabe.signals import SignalStore, WorkStopped

DAY = 86400
RESULT_INTERVAL = 300
DELAYED_RESULT_INTERVAL = 3600
METRICS = ("eps", "revenue")


def number(value, *, revenue=False):
    if value is None or isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value))
        if not result.is_finite() or abs(result) > Decimal("1e18") or (revenue and result < 0):
            return None
        return str(result)
    except (InvalidOperation, ValueError, TypeError):
        return None


def amount(value, *, revenue=False):
    if value is None:
        return "unavailable"
    value = Decimal(value)
    if revenue:
        for scale, suffix in (("1e12", "T"), ("1e9", "B"), ("1e6", "M"), ("1e3", "K")):
            if abs(value) >= Decimal(scale):
                return f"{value / Decimal(scale):.2f}{suffix}"
    return f"{value:.4f}".rstrip("0").rstrip(".") if value else "0"


def comparison(actual, estimate):
    if actual is None or estimate is None:
        return None
    actual, estimate = Decimal(actual), Decimal(estimate)
    delta = actual - estimate
    return (
        "HIT" if delta > 0 else "MISS" if delta < 0 else "MET",
        delta / abs(estimate) * 100 if estimate else None,
    )


def metric_line(metric, result):
    actual, estimate = result.get("actual"), result.get("estimate")
    label = "EPS" if metric == "eps" else "Revenue"
    if result.get("source") == "yahoo-news":
        method = result["basis"].split()[0]
        return f"EPS ({method}): {amount(actual)} {result['currency']} • reported in news"
    verdict = comparison(actual, estimate)
    text = f"{label}: {amount(actual, revenue=metric == 'revenue')} vs {amount(estimate, revenue=metric == 'revenue')} expected"
    if metric == "revenue" and result.get("source") == "alphavantage":
        text += " • " + (result.get("currency") or "currency unavailable")
    if not verdict:
        return text
    outcome, surprise = verdict
    icon = {"HIT": "🟢", "MISS": "🔴", "MET": "⚪"}[outcome]
    return f"{icon} {outcome} • {text}" + (
        f" ({surprise:+.1f}%)" if surprise is not None else " (zero estimate; % unavailable)"
    )


def expectations(payload):
    return (
        f"Expected: EPS {amount(payload.get('epsEstimate'))} • "
        f"Revenue {amount(payload.get('revenueEstimate'), revenue=True)}"
    )


def calendar_estimates(payload, source=None):
    """Display saved forecasts as context without pairing different EPS bases."""
    if all(payload.get(field) is None for field in ("epsEstimate", "revenueEstimate")):
        return None
    name = {"finnhub": "Finnhub", "alphavantage": "Alpha Vantage"}.get(source)
    label = f"Calendar estimates ({name})" if name else "Saved calendar estimates"
    return (
        f"{label}: EPS {amount(payload.get('epsEstimate'))} • "
        f"Revenue {amount(payload.get('revenueEstimate'), revenue=True)}"
    )


def result_window(payload, now):
    """Poll around the expected release, with seven days for delayed feeds."""
    try:
        day = date.fromisoformat(payload["date"])
        local = datetime.fromtimestamp(now, NY)
        distance = (local.date() - day).days
        if not 0 <= distance <= 7:
            return False
        if distance:
            return 7 <= local.hour < 22
        session = USCalendar().session(day)
        if session is None:
            return False
        hour = payload.get("hour")
        start = (
            session.opens - 2 * 3600
            if hour == "before-open"
            else session.closes - 1800
            if hour == "after-close"
            else session.opens - 2 * 3600
        )
        return start <= now and local.hour < 22
    except (KeyError, ValueError):
        return False


class EarningsResults:
    def __init__(self, store, http):
        self.store, self.http = store, http
        self.repo = SignalStore(store)

    def history(self, symbol):
        cached = self.repo.get("earnings-history", symbol)
        return cached["payload"].get("rows", []) if cached else []

    def refresh(self, symbol, *, now, interval=DAY, force=False):
        if not force and not self.repo.due("earnings", "finnhub-history", symbol, interval, now):
            return
        self.repo.attempt("earnings", "finnhub-history", symbol, now)
        try:
            raw = self.http.finnhub("stock/earnings", symbol=symbol, limit=4)
            now = max(now, getattr(self.http, "received_at", now))
            if not isinstance(raw, list):
                raise ApiError("Quarterly EPS history unavailable.")
            rows = []
            for item in raw[:100]:
                if not isinstance(item, dict) or item.get("symbol") != symbol:
                    continue
                try:
                    period = date.fromisoformat(item["period"])
                    year, quarter = item.get("year"), item.get("quarter")
                    if (
                        period > datetime.fromtimestamp(now, NY).date()
                        or type(year) is not int
                        or not 1900 <= year <= 2200
                        or type(quarter) is not int
                        or not 1 <= quarter <= 4
                    ):
                        continue
                    rows.append(
                        {
                            "year": year,
                            "quarter": quarter,
                            "period": period.isoformat(),
                            "actual": number(item.get("actual")),
                            "estimate": number(item.get("estimate")),
                        }
                    )
                except (KeyError, TypeError, ValueError):
                    continue
            if raw and not rows:
                raise ApiError("Quarterly EPS history contained no valid rows.")
            if rows:
                unique = {(r["year"], r["quarter"]): r for r in rows}
                self.repo.put(
                    "earnings-history",
                    symbol,
                    symbol,
                    "finnhub",
                    {
                        "rows": sorted(unique.values(), key=lambda r: r["period"], reverse=True)[
                            :4
                        ],
                        "checked_at": now,
                    },
                    now=now,
                    event_time=now,
                )
            self.repo.ok("earnings", "finnhub-history", symbol, now)
        except WorkStopped:
            raise
        except Exception:
            self.repo.fail(
                "earnings",
                "finnhub-history",
                symbol,
                "Quarterly EPS history unavailable; cache retained",
                now,
                retry=min(interval, 3600),
            )

    def fallback(self, symbol, *, now, force=False):
        """Use independently paired EPS and exact-period revenue for recent gaps."""
        if self.store.state("earnings_alpha_disabled", "1") != "0":
            return
        if not getattr(getattr(self.http, "config", None), "api_key", ""):
            return
        today = datetime.fromtimestamp(now, NY).date()
        records = [
            row
            for row in self.repo.rows("earnings", [symbol], limit=100)
            if not any(
                row["payload"].get(k) for k in ("superseded_by", "conflict", "source_conflicts")
            )
            and 0 <= (today - date.fromisoformat(row["payload"]["date"])).days <= 7
            and (force or result_window(row["payload"], now))
        ]
        if not records:
            return
        for metric, function, rows_key in (
            ("eps", "EARNINGS", "quarterlyEarnings"),
            ("revenue", "INCOME_STATEMENT", "quarterlyReports"),
        ):
            missing = [
                r
                for r in records
                if r["payload"].get("comparisons", {}).get(metric, {}).get("actual") is None
            ]
            if not missing or (
                metric == "revenue" and not any(r["payload"].get("fiscal_end") for r in missing)
            ):
                continue
            source = "alphavantage-" + metric
            if not force and not self.repo.due("earnings", source, symbol, 6 * 3600, now):
                continue
            self.repo.attempt("earnings", source, symbol, now)
            try:
                raw = self.http.alpha_json(function, symbol=symbol)
                checked = getattr(self.http, "alpha_checked_at", now)
                now = max(now, checked, getattr(self.http, "received_at", now))
                if (
                    not isinstance(raw, dict)
                    or raw.get("symbol") != symbol
                    or not isinstance(raw.get(rows_key), list)
                ):
                    raise ApiError("Invalid earnings fallback response.")
                for row in missing:
                    payload = row["payload"]
                    matches = []
                    for item in raw[rows_key][:100]:
                        if not isinstance(item, dict):
                            continue
                        try:
                            fiscal = date.fromisoformat(item["fiscalDateEnding"])
                            release = date.fromisoformat(payload["date"])
                            if not 0 <= (release - fiscal).days <= 180:
                                continue
                            if metric == "eps":
                                if item.get("reportedDate") != payload["date"] or (
                                    payload.get("fiscal_end")
                                    and payload["fiscal_end"] != fiscal.isoformat()
                                ):
                                    continue
                            elif fiscal.isoformat() != payload.get("fiscal_end"):
                                continue
                            matches.append(item)
                        except (KeyError, TypeError, ValueError):
                            continue
                    if len(matches) != 1:
                        continue
                    item = matches[0]
                    actual = number(
                        item.get("reportedEPS" if metric == "eps" else "totalRevenue"),
                        revenue=metric == "revenue",
                    )
                    if actual is None:
                        continue
                    payload["fiscal_end"] = item["fiscalDateEnding"]
                    estimate = number(item.get("estimatedEPS")) if metric == "eps" else None
                    payload.setdefault("comparisons", {})[metric] = {
                        "actual": actual,
                        "estimate": estimate,
                        "basis": "alphavantage-" + function.lower(),
                        "source": "alphavantage",
                        "fiscal_end": item["fiscalDateEnding"],
                        "checked_at": checked,
                        "currency": item.get("reportedCurrency")
                        if re.fullmatch(r"[A-Z]{3}", str(item.get("reportedCurrency", "")))
                        else None,
                    }
                    payload.setdefault("result_seen", {})
                    if comparison(actual, estimate):
                        payload["result_seen"].setdefault(metric, now)
                    payload["result_tracking"] = payload.get(
                        "result_tracking", not payload.get("reported")
                    )
                    payload["reported"] = True
                    self.repo.put(
                        "earnings",
                        row["key"],
                        symbol,
                        row["source"],
                        payload,
                        now=now,
                        event_time=row["event_time"],
                        available_at=row["available_at"],
                    )
                self.repo.ok("earnings", source, symbol, now)
            except WorkStopped:
                raise
            except Exception:
                self.repo.fail(
                    "earnings",
                    source,
                    symbol,
                    "Alpha Vantage results unavailable; cached figures retained",
                    now,
                    retry=6 * 3600,
                )

    @staticmethod
    def track(previous, payload, now):
        """Preserve known actuals and never seed historical result notifications."""
        payload["result_tracking"] = previous.get(
            "result_tracking", not previous.get("reported") if previous else not payload["reported"]
        )
        payload["result_seen"] = dict(previous.get("result_seen", {}))
        payload["comparisons"] = dict(previous.get("comparisons", {}))
        if previous.get("withdrawn_results"):
            payload["withdrawn_results"] = dict(previous["withdrawn_results"])
        if previous.get("fiscal_end") and not payload.get("fiscal_end"):
            payload["fiscal_end"] = previous["fiscal_end"]
        for metric in METRICS:
            for suffix in ("Estimate", "Actual"):
                key = metric + suffix
                if payload.get(key) is None and previous.get(key) is not None:
                    payload[key] = previous[key]
            if payload.get(metric + "Actual") is not None:
                result = {
                    "actual": payload[metric + "Actual"],
                    "estimate": payload.get(metric + "Estimate"),
                    "basis": "calendar",
                    "checked_at": now,
                }
                payload["comparisons"][metric] = result
                if comparison(result["actual"], result["estimate"]):
                    payload["result_seen"].setdefault(metric, now)
        payload["reported"] = payload["reported"] or bool(payload["comparisons"])

    def attach(self, symbol, *, now):
        for row in self.repo.rows("earnings", [symbol], limit=100):
            payload = row["payload"]
            if payload.get("superseded_by"):
                continue
            match = re.fullmatch(r"FY(\d{4}) Q([1-4])", payload.get("period", ""))
            fiscal_end = payload.get("fiscal_end")
            history = next(
                (
                    r
                    for r in self.history(symbol)
                    if (match and (r["year"], r["quarter"]) == (int(match[1]), int(match[2])))
                    or (fiscal_end and r["period"] == fiscal_end)
                ),
                None,
            )
            if not history or history["actual"] is None:
                continue
            if (
                abs(
                    (
                        datetime.fromtimestamp(now, NY).date() - date.fromisoformat(payload["date"])
                    ).days
                )
                > 7
            ):
                continue
            cached = self.repo.get("earnings-history", symbol)
            if not cached or now - cached["payload"]["checked_at"] > DAY:
                continue
            incoming = {
                "actual": history["actual"],
                "estimate": history["estimate"],
                "basis": "quarterly-history",
                "checked_at": cached["payload"]["checked_at"],
            }
            if payload.get("reported") and payload.get("comparisons", {}).get("eps") == incoming:
                continue
            payload["result_tracking"] = payload.get("result_tracking", not payload.get("reported"))
            payload["comparisons"] = dict(payload.get("comparisons", {}))
            payload["comparisons"]["eps"] = incoming
            payload["result_seen"] = dict(payload.get("result_seen", {}))
            if comparison(history["actual"], history["estimate"]):
                payload["result_seen"].setdefault("eps", cached["payload"]["checked_at"])
            payload["reported"] = True
            self.repo.put(
                "earnings",
                row["key"],
                symbol,
                row["source"],
                payload,
                now=now,
                event_time=row["event_time"],
                available_at=row["available_at"],
            )

    def alerts(self, symbols, *, now):
        watermark = max(
            float(self.store.state(key, "0"))
            for key in (
                "earnings_enabled_at",
                "insights_resumed_at",
                "pause_until",
            )
        )
        today = datetime.fromtimestamp(now, NY).date()
        for row in self.repo.rows("earnings", symbols, limit=2000):
            p = row["payload"]
            if (
                not p.get("result_tracking")
                or p.get("superseded_by")
                or p.get("conflict")
                or p.get("source_conflicts")
            ):
                continue
            if not -1 <= (today - date.fromisoformat(p["date"])).days <= 7:
                continue
            base = "result:" + row["key"]
            eps = p.get("comparisons", {}).get("eps", {})
            seen = p.get("result_seen", {}).get("eps", 0)
            if (
                eps.get("source") == "yahoo-news"
                and eps.get("actual") is not None
                and watermark <= seen <= now
                and now - eps.get("checked_at", 0) <= DAY
            ):
                withdrawn = p.get("withdrawn_results", {}).get("eps")
                suffix = (
                    hashlib.sha256(str(withdrawn.get("checked_at")).encode()).hexdigest()[:16]
                    if withdrawn
                    else "news"
                )
                key = base + ":eps:" + suffix
                if not self.repo.notified("earnings", key):
                    title = "Earnings correction" if withdrawn else "Earnings published"
                    lines = [
                        f"📊 {title} • {row['symbol']} • {p['period']}",
                        metric_line("eps", eps),
                    ]
                    saved = calendar_estimates(p, row.get("source"))
                    if saved:
                        lines.append(saved)
                    lines.append("Yahoo news • estimate basis not matched; no beat/miss grade.")
                    yield key, "\n".join(lines)
            pending = []
            for metric in METRICS:
                data = p.get("comparisons", {}).get(metric, {})
                seen = p.get("result_seen", {}).get(metric, 0)
                if (
                    seen < watermark
                    or seen > now
                    or now - data.get("checked_at", 0) > DAY
                    or not comparison(data.get("actual"), data.get("estimate"))
                    or self.repo.notified("earnings", base + ":" + metric)
                    or self.repo.notified("earnings", base + ":eps+revenue")
                ):
                    continue
                pending.append(metric)
            if pending:
                text = f"📊 Earnings update • {row['symbol']} • {p['period']}\n"
                text += "\n".join(
                    metric_line(metric, p["comparisons"][metric]) for metric in pending
                )
                if pending == ["eps"] and "revenue" not in p.get("comparisons", {}):
                    text += "\nRevenue: awaiting provider results."
                elif pending == ["eps"] and p["comparisons"]["revenue"].get("estimate") is None:
                    text += "\n" + metric_line("revenue", p["comparisons"]["revenue"])
                if any(
                    p["comparisons"][metric].get("source") == "alphavantage" for metric in pending
                ):
                    text += "\nAlpha Vantage fallback • EPS uses its own estimate."
                yield base + ":" + "+".join(pending), text
