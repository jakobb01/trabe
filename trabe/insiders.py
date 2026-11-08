"""Reported executive/director purchases with explicit filing qualification."""

import hashlib
import json
import re
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from urllib.parse import urlencode, urlsplit

from trabe.signals import SignalStore, WorkStopped

CACHE_SECONDS = 6 * 3600
RECENT_SECONDS = 7 * 86400
MAX_PURCHASES = 500
TRACEFOUR = "https://tracefour.com/v1/filings"
ATTRIBUTION = "https://tracefour.com/filings"
FLAGS = (
    "isPairedExercise",
    "hasOfferingFootnote",
    "hasTaxWithholdingFootnote",
    "hasAdsFootnote",
    "hasIndirectOwnershipFootnote",
    "hasVwapFootnote",
    "is10b5_1",
)


def _text(value, maximum=160):
    return re.sub(r"[\x00-\x1f\x7f]", " ", str(value or "")).strip()[:maximum]


def _number(value):
    if isinstance(value, bool):
        raise ValueError("Invalid purchase amount.")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError("Invalid purchase amount.") from None
    if not result.is_finite() or result <= 0 or result.adjusted() > 18:
        raise ValueError("Invalid purchase amount.")
    return result


def _timestamp(value):
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Publication timezone is missing.")
    return parsed.timestamp()


def _checked(timestamp, now):
    if not timestamp:
        return "not checked"
    return datetime.fromtimestamp(timestamp, UTC).strftime("%d %b %H:%M UTC") + (
        " • stale coverage" if now - timestamp > CACHE_SECONDS * 2 else ""
    )


def _date(value):
    parsed = date.fromisoformat(str(value))
    if parsed.isoformat() != value:
        raise ValueError("Invalid purchase date.")
    return parsed


def _name(value):
    return " ".join(sorted(re.findall(r"[A-Z0-9]+", str(value or "").upper())))


def _accession(value):
    match = re.match(r"^(\d{10}-\d{2}-\d{6})(?:-\d+)?$", str(value or ""))
    return match[1] if match else ""


def _link(value, hosts):
    value = str(value or "")
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or parsed.hostname not in hosts
        or parsed.username
        or parsed.password
        or parsed.port not in (None, 443)
        or len(value) > 1200
    ):
        return ""
    return value


def _role(raw):
    title = _text(raw.get("ownerTitle"))
    normalized = title.upper().replace("-", " ")
    roles = []
    if raw.get("isDirector") is True:
        roles.append("director")
    if raw.get("isOfficer") is True and not re.search(r"\b(FORMER|RETIRED)\b", normalized):
        if re.search(r"\bCEO\b|\bCHIEF\s+EXECUTIVE\s+OFFICER\b", normalized):
            roles.append("CEO")
        if re.search(r"\bCFO\b|\bCHIEF\s+FINANCIAL\s+OFFICER\b", normalized):
            roles.append("CFO")
    return title, roles


def _match(raw, candidates):
    """Accession, owner, date, shares and price must agree, not just a name."""
    accession = _accession(raw.get("filingId"))
    if not accession:
        return "unknown"
    matches = []
    for candidate in candidates:
        try:
            if (
                candidate.get("symbol") != raw["issuerTicker"]
                or candidate.get("transactionCode") != "P"
                or _accession(candidate.get("id")) != accession
                or _name(candidate.get("name")) != _name(raw.get("ownerName"))
                or candidate.get("transactionDate") != raw.get("transactionDate")
            ):
                continue
            shares = _number(raw["shares"])
            price = _number(raw["pricePerShare"])
            if abs(_number(candidate["change"]) - shares) > max(
                Decimal("0.000001"), shares * Decimal("0.000001")
            ):
                continue
            if abs(_number(candidate["transactionPrice"]) - price) > max(
                Decimal("0.0001"), price * Decimal("0.00001")
            ):
                continue
            matches.append(candidate.get("isDerivative"))
        except (ValueError, KeyError, TypeError, InvalidOperation):
            continue
    if any(value is True for value in matches):
        return "derivative"
    return "non-derivative" if any(value is False for value in matches) else "unknown"


def normalize(raw, candidates, symbol, now):
    """Keep unknown qualification visible; never promote it to a verified buy."""
    if not isinstance(raw, dict) or raw.get("issuerTicker") != symbol:
        return None
    try:
        shares, price = _number(raw["shares"]), _number(raw["pricePerShare"])
        transaction = _date(raw["transactionDate"])
        published = _timestamp(raw["filedAt"])
        filing_id = _text(raw["filingId"], 100)
        if published > now + 2 or published <= 0 or not filing_id:
            return None
        value = shares * price
        title, roles = _role(raw)
        derivative = _match(raw, candidates)
        errors = []
        if not _name(raw.get("ownerName")):
            errors.append("reporting owner name unavailable")
        if raw.get("transactionCode") != "P":
            errors.append("not a P purchase")
        if not roles:
            errors.append("CEO/CFO/director role not verified")
        if derivative != "non-derivative":
            errors.append(
                "derivative transaction"
                if derivative == "derivative"
                else "non-derivative eligibility not verified"
            )
        for field, reason in (
            ("isPairedExercise", "paired exercise"),
            ("hasOfferingFootnote", "offering/private-placement context"),
            ("hasTaxWithholdingFootnote", "tax-withholding context"),
        ):
            if raw.get(field) is True:
                errors.append(reason)
        form = _text(raw.get("formType"), 10)
        amendment = form == "4/A"
        if form not in ("4", "4/A"):
            errors.append("Form 4 status not verified")
        if amendment:
            errors.append("amendment/correction; no new purchase alert")
        published_date = datetime.fromtimestamp(published, UTC).date()
        if not 0 <= (published_date - transaction).days <= 90:
            errors.append("transaction date outside recent-purchase window")
        if "totalValue" in raw:
            reported = _number(raw["totalValue"])
            if abs(reported - value) > max(Decimal("0.01"), value * Decimal("0.01")):
                errors.append("reported purchase value is inconsistent")
        source = _link(raw.get("rawFilingUrl"), {"www.sec.gov", "sec.gov"})
        if not source or not urlsplit(source).path.startswith("/Archives/edgar/"):
            source = ""
            errors.append("original SEC filing link unavailable")
        attribution = _link(raw.get("url"), {"tracefour.com", "www.tracefour.com"}) or ATTRIBUTION
        economic = "|".join(
            (
                symbol,
                _name(raw.get("ownerName")),
                transaction.isoformat(),
                str(shares.normalize()),
                str(price.normalize()),
            )
        )
        return {
            "filing_id": filing_id,
            "accession": _accession(filing_id),
            "owner": _text(raw.get("ownerName")),
            "title": title,
            "roles": roles,
            "shares": str(shares),
            "price": str(price),
            "value_usd": str(value),
            "currency": "USD",
            "transaction_code": _text(raw.get("transactionCode"), 4),
            "transaction_date": transaction.isoformat(),
            "published_at": published,
            "publication_precision": "timestamp",
            "transaction_precision": "date",
            "derivative_status": derivative,
            "derivative_source": "Finnhub matching transaction"
            if derivative != "unknown"
            else "unavailable",
            "eligibility_errors": errors,
            "amendment": amendment,
            "form_type": form,
            "original_submission_date": _text(raw.get("originalSubmissionDate"), 10),
            "flags": {field: raw.get(field) is True for field in FLAGS},
            "source_url": source,
            "attribution_url": attribution,
            "purchase_classification": "reported P purchase: open-market/private",
            "economic_key": hashlib.sha256(economic.encode()).hexdigest(),
        }
    except (ValueError, KeyError, TypeError, InvalidOperation, OverflowError):
        return None


class Insiders:
    def __init__(self, store, http):
        self.store, self.http = store, http
        self.repo = SignalStore(store)

    def _transactions(self, symbol, today):
        """Keep every bounded response and split crowded date ranges, newest first."""
        ranges = [(today - timedelta(days=90), today)]
        candidates, requests, partial = {}, 0, False
        while ranges and requests < 7:
            start, end = ranges.pop()
            requests += 1
            try:
                response = self.http.finnhub(
                    "stock/insider-transactions",
                    symbol=symbol,
                    **{"from": start.isoformat(), "to": end.isoformat()},
                )
                if not isinstance(response, dict) or not isinstance(response.get("data"), list):
                    raise ValueError("Invalid transaction response.")
            except WorkStopped:
                raise
            except Exception:
                if requests == 1:
                    raise
                partial = True
                continue
            for row in response["data"][:2000]:
                if isinstance(row, dict) and row.get("symbol") == symbol:
                    identity = json.dumps(row, sort_keys=True)
                    if identity in candidates or len(candidates) < 2000:
                        candidates[identity] = row
                    else:
                        partial = True
            if len(response["data"]) >= 100:
                if start == end:
                    partial = True
                else:
                    middle = start + (end - start) // 2
                    ranges.extend(((start, middle), (middle + timedelta(days=1), end)))
        return list(candidates.values()), partial or bool(ranges), requests

    def refresh(self, symbols, *, now, force=False):
        today = datetime.fromtimestamp(now, UTC).date()
        for symbol in sorted(set(symbols)):
            if not force and not any(
                self.repo.due("insiders", source, symbol, CACHE_SECONDS, now)
                for source in ("tracefour", "finnhub")
            ):
                continue
            if not re.fullmatch(r"[A-Z]{1,5}", symbol):
                self.repo.fail(
                    "insiders",
                    "tracefour",
                    symbol,
                    "Tracefour supports 1–5 letter US tickers; this symbol is unsupported.",
                    now,
                    retry=CACHE_SECONDS,
                )
                continue
            primary = None
            self.repo.attempt("insiders", "tracefour", symbol, now)
            try:
                url = (
                    TRACEFOUR
                    + "?"
                    + urlencode(
                        {
                            "ticker": symbol,
                            "direction": "P",
                            "since": (today - timedelta(days=90)).isoformat(),
                            "limit": MAX_PURCHASES,
                        }
                    )
                )
                envelope = self.http.json(url, provider="tracefour")
                if (
                    not isinstance(envelope, dict)
                    or envelope.get("error")
                    or not isinstance(envelope.get("data"), list)
                ):
                    raise ValueError("Invalid filing response.")
                primary = envelope["data"][:MAX_PURCHASES]
                trace_received = max(now, getattr(self.http, "received_at", now))
                self.repo.ok(
                    "insiders",
                    "tracefour",
                    symbol,
                    trace_received,
                )
                self.store.set_state(
                    "insiders_warning_" + symbol,
                    "Source response reached its 500-row cap; coverage may be partial."
                    if len(envelope["data"]) >= MAX_PURCHASES
                    else "",
                )
            except WorkStopped:
                raise
            except Exception:
                self.repo.fail(
                    "insiders",
                    "tracefour",
                    symbol,
                    "Tracefour unavailable or quota-limited; cached filings retained.",
                    now,
                )
            candidates = []
            self.repo.attempt("insiders", "finnhub", symbol, now)
            try:
                candidates, partial, requests = self._transactions(symbol, today)
                self.store.set_state(
                    "insiders_finnhub_warning_" + symbol,
                    "Finnhub verification coverage may be incomplete; a crowded or failed date range remains."
                    if partial
                    else "",
                )
                received = max(now, getattr(self.http, "received_at", now))
                coverage = self.repo.get("insider-coverage", symbol)
                details = dict(coverage["payload"]) if coverage else {}
                details.update(
                    {
                        "from": (today - timedelta(days=90)).isoformat(),
                        "to": today.isoformat(),
                        "transactions": len(candidates),
                        "purchases": sum(c.get("transactionCode") == "P" for c in candidates),
                        "partial": partial,
                        "requests": requests,
                        "checked_at": received,
                    }
                )
                self.repo.put(
                    "insider-coverage",
                    symbol,
                    symbol,
                    "finnhub",
                    details,
                    now=received,
                    event_time=received,
                )
                self.repo.ok(
                    "insiders", "finnhub", symbol, max(now, getattr(self.http, "received_at", now))
                )
            except WorkStopped:
                raise
            except Exception:
                self.repo.fail(
                    "insiders",
                    "finnhub",
                    symbol,
                    "Finnhub transaction verification unavailable; unverified purchases cannot alert.",
                    now,
                )
            if primary is not None:
                received = max(now, getattr(self.http, "received_at", now))
                values = [
                    value
                    for raw in primary
                    if (value := normalize(raw, candidates, symbol, trace_received))
                ]
                coverage = self.repo.get("insider-coverage", symbol)
                details = dict(coverage["payload"]) if coverage else {}
                details.update(
                    trace_purchases=len(primary),
                    trace_checked_at=trace_received,
                    trace_rejected=len(primary) - len(values),
                    verified=sum(self._qualified(v) for v in values),
                    unverified=sum(not self._qualified(v) for v in values),
                )
                self.repo.put(
                    "insider-coverage",
                    symbol,
                    symbol,
                    "Tracefour + Finnhub",
                    details,
                    now=received,
                    event_time=received,
                )
                for value in sorted(values, key=lambda value: value["published_at"], reverse=True)[
                    :MAX_PURCHASES
                ]:
                    value["source_fetched_at"] = trace_received
                    key = symbol + ":" + value["filing_id"]
                    old = self.repo.get("insiders", key)
                    first_seen = old["observed_at"] if old else received
                    value["first_seen_at"] = first_seen
                    self.repo.put(
                        "insiders",
                        key,
                        symbol,
                        "Tracefour + Finnhub",
                        value,
                        now=received,
                        event_time=datetime.combine(
                            _date(value["transaction_date"]), time.min, UTC
                        ).timestamp(),
                        available_at=max(value["published_at"], first_seen),
                    )
                self._reconcile_amendments(symbol, received)
                retained = self.repo.rows("insiders", [symbol], limit=10000)
                for row in retained[MAX_PURCHASES:]:
                    self.repo.delete("insiders", row["key"])

    def _reconcile_amendments(self, symbol, now):
        rows = self.repo.rows("insiders", [symbol], limit=1000)
        for amendment in (r for r in rows if r["payload"].get("amendment")):
            changed = amendment["payload"]
            for original in rows:
                prior = original["payload"]
                if prior["amendment"] or prior.get("amended_by") == changed["filing_id"]:
                    continue
                same_economics = prior["economic_key"] == changed["economic_key"]
                original_day = datetime.fromtimestamp(prior["published_at"], UTC).date().isoformat()
                dated_correction = (
                    changed["original_submission_date"] == original_day
                    and _name(changed["owner"]) == _name(prior["owner"])
                    and changed["transaction_date"] == prior["transaction_date"]
                )
                if not (same_economics or dated_correction):
                    continue
                prior = dict(prior)
                prior["eligibility_errors"] = list(prior["eligibility_errors"])
                prior["eligibility_errors"].append(
                    "superseded by amendment; review corrected filing"
                )
                prior["amended_by"] = changed["filing_id"]
                self.repo.put(
                    "insiders",
                    original["key"],
                    symbol,
                    original["source"],
                    prior,
                    now=now,
                    event_time=original["event_time"],
                    available_at=original["available_at"],
                )

    def _qualified(self, payload):
        minimum = Decimal(self.store.settings().get("insider_min_value_usd", "10000"))
        return not payload["eligibility_errors"] and Decimal(payload["value_usd"]) >= minimum

    def _rows(self, symbols):
        return self.repo.rows("insiders", symbols, limit=5000)

    def _line(self, row, *, compact=False):
        value = row["payload"]
        role = "/".join(value["roles"]) or value["title"] or "role unknown"
        status = (
            "✅ Qualified"
            if self._qualified(value)
            else "⚠️ Unqualified: "
            + (", ".join(value["eligibility_errors"]) or "below configured value threshold")
        )
        published = datetime.fromtimestamp(value["published_at"], UTC).strftime(
            "%Y-%m-%d %H:%M UTC"
        )
        context = [
            name
            for field, name in (
                ("is10b5_1", "10b5-1 plan"),
                ("hasIndirectOwnershipFootnote", "indirect ownership"),
                ("hasAdsFootnote", "ADS"),
                ("hasVwapFootnote", "weighted execution price"),
            )
            if value["flags"].get(field)
        ]
        links = (
            f"SEC: {value['source_url']}\n"
            if value["source_url"]
            else "SEC filing link unavailable\n"
        ) + f"Tracefour (CC BY 4.0): {value['attribution_url']}"
        if compact:
            return (
                f"{row['symbol']} • {Decimal(value['value_usd']):,.2f} USD • {role}\n"
                f"{value['owner']} • traded {value['transaction_date']} • filed {published}\n"
                f"{value['purchase_classification']}\n" + links
            )
        return (
            f"{row['symbol']} • {value['owner']} ({role})\n"
            f"Value: {Decimal(value['value_usd']):,.2f} USD • {Decimal(value['shares']):,f} shares × {Decimal(value['price']):,.4f} USD\n"
            f"Traded: {value['transaction_date']} • Filed: {published}\n"
            f"{value['purchase_classification']} • {status}\n"
            + ("Context: " + ", ".join(context) + "\n" if context else "")
            + links
        )

    def report(self, symbols, *, now):
        rows = self._rows(symbols)
        result = ["🚨 Insider purchases"]
        result.append(
            "P purchases only • CEO/CFO/director • minimum "
            + self.store.settings()["insider_min_value_usd"]
            + " USD"
        )
        if not rows:
            result.append("No purchases cached in the inspected window.")
        result.extend(
            self._line(row)
            for row in sorted(rows, key=lambda row: row["payload"]["published_at"], reverse=True)[
                :5
            ]
        )
        if len(rows) > 5:
            result.append(f"Showing 5 of {len(rows)} cached purchases. More: /insiders TICKER")
        for status in self.repo.status("insiders"):
            if status["symbol"] in symbols and status["error"]:
                result.append(f"{status['symbol']} {status['source']}: {status['error']}")
        for symbol in symbols:
            coverage = self.repo.get("insider-coverage", symbol)
            if coverage:
                detail = coverage["payload"]
                cached = [row for row in rows if row["symbol"] == symbol]
                qualified = sum(self._qualified(row["payload"]) for row in cached)
                result.append(
                    f"{symbol} • 90-day coverage\n"
                    f"Tracefour: {detail.get('trace_purchases', 'unavailable')} purchase rows • checked {_checked(detail.get('trace_checked_at'), now)}\n"
                    f"Finnhub: {detail.get('purchases', 'unavailable')} P transactions / {detail.get('transactions', 'unavailable')} total • checked {_checked(detail.get('checked_at'), now)}\n"
                    f"Verified eligible: {qualified} • unverified/excluded: {len(cached) - qualified} • cached: {len(cached)}"
                )
                if detail.get("trace_rejected"):
                    result.append(
                        f"{symbol}: Unusable filing rows: {detail['trace_rejected']}; purchase coverage may be incomplete."
                    )
                if detail.get("purchases", 0) > detail.get("trace_purchases", 0):
                    result.append(
                        f"{symbol}: Finnhub has more P transactions than Tracefour; role/filing coverage may be incomplete."
                    )
            else:
                result.append(f"{symbol}: coverage has not been checked yet.")
            for prefix in ("insiders_warning_", "insiders_finnhub_warning_"):
                warning = self.store.state(prefix + symbol)
                if warning:
                    result.append(f"{symbol}: {warning}")
        if rows:
            checked = max(row["last_seen"] for row in rows)
            result.append(
                f"Last observed: {datetime.fromtimestamp(checked, UTC).strftime('%d %b %H:%M UTC')}"
                + (" • stale coverage" if now - checked > CACHE_SECONDS * 2 else "")
            )
        result.append("Reported purchases; open-market/private does not confirm an exchange venue.")
        return "\n\n".join(result)

    def alerts(self, symbols, *, now):
        if self.store.settings().get(
            "insiders_enabled", "off"
        ) != "on" or self.store.automation_paused(now):
            return []
        activated = float(self.store.state("insiders_enabled_at", "0"))
        if activated <= 0:
            return []
        watermark = max(
            activated,
            float(self.store.state("pause_until", "0")),
            float(self.store.state("insights_resumed_at", "0")),
        )
        alerts, seen = [], set()
        for row in sorted(self._rows(symbols), key=lambda row: row["payload"]["published_at"]):
            value = row["payload"]
            key = "purchase:" + value["economic_key"]
            if (
                key in seen
                or not self._qualified(value)
                or not watermark <= value["published_at"] <= now
                or now - value["published_at"] > RECENT_SECONDS
                or now - row["last_seen"] > CACHE_SECONDS * 2
                or self.repo.notified("insiders", key)
            ):
                continue
            seen.add(key)
            alerts.append((key, "🚨 Insider purchase detected\n" + self._line(row)))
        return alerts

    def briefing(self, symbols, *, now, limit=3):
        rows = [
            row
            for row in self._rows(symbols)
            if self._qualified(row["payload"])
            and 0 <= now - row["payload"]["published_at"] <= RECENT_SECONDS
        ]
        lines = []
        for row in sorted(rows, key=lambda row: row["payload"]["published_at"], reverse=True)[
            :limit
        ]:
            lines.append(self._line(row, compact=True))
        return lines or [
            "No recent qualified insider purchases cached; coverage may be unavailable."
        ]
