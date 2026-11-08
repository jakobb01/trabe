"""Cached, source-qualified earnings schedules and stable warning identities."""

import csv
import io
import re
from datetime import UTC, date, datetime, timedelta

from trabe.api import ApiError
from trabe.calendar import NY, USCalendar
from trabe.earnings_results import (
    DELAYED_RESULT_INTERVAL,
    RESULT_INTERVAL,
    EarningsResults,
    calendar_estimates,
    comparison,
    expectations,
    metric_line,
    number,
    result_window,
)
from trabe.portfolio import Portfolio
from trabe.signals import SignalStore, WorkStopped

KIND = "earnings"
DAY = 86400
SOURCES = {
    "finnhub": "https://finnhub.io/docs/api/earnings-calendar",
    "alphavantage": "https://www.alphavantage.co/documentation/#earnings-calendar",
}


def _day(value):
    if not isinstance(value, str):
        raise ValueError("Missing earnings date.")
    parsed = date.fromisoformat(value)
    if parsed.isoformat() != value:
        raise ValueError("Invalid earnings date.")
    return parsed


def _hour(value):
    text = re.sub(r"[\s_-]+", "", str(value or "").lower())
    return {
        "bmo": "before-open",
        "premarket": "before-open",
        "beforemarketopen": "before-open",
        "beforeopen": "before-open",
        "amc": "after-close",
        "postmarket": "after-close",
        "aftermarketclose": "after-close",
        "afterclose": "after-close",
        "dmh": "during-session",
        "duringmarkethours": "during-session",
    }.get(text, "unknown")


class Earnings:
    def __init__(self, store, http):
        self.store, self.http = store, http
        self.signals = SignalStore(store)
        self.results = EarningsResults(store, http)

    def _records(self, symbols=None):
        if symbols is not None and not symbols:
            return []
        return self.signals.rows(KIND, symbols, limit=2000)

    def _status(self, symbol, text):
        self.store.set_state("earnings_status_" + symbol, text)

    def _received(self, now):
        return max(now, getattr(self.http, "received_at", now))

    def _store_event(self, symbol, source, item, now):
        """Attach source aliases without treating an uncertain match as a revision."""
        records = self._records([symbol])
        alias = item["alias"]
        matched = next((r for r in records if alias in r["payload"].get("aliases", [])), None)
        if matched is None:
            matched = next(
                (
                    r
                    for r in records
                    if not r["payload"].get("superseded_by")
                    and r["payload"].get("date") == item["date"]
                    and r["source"] != source
                ),
                None,
            )
        key = matched["key"] if matched else alias
        previous = matched["payload"] if matched else {}
        # A matching fallback is an alias, not a downgrade of a fresh primary.
        if source == "alphavantage" and matched and matched["source"] == "finnhub":
            preserved = dict(previous)
            preserved["aliases"] = sorted(set(previous.get("aliases", [])) | {alias})
            preserved["fallback_checked_at"] = now
            if previous.get("date") != item["date"]:
                preserved["source_conflicts"] = [{"source": source, "date": item["date"]}]
                preserved["related_dates"] = sorted(
                    set(previous.get("related_dates", [])) | {item["date"]}
                )
            self.signals.put(
                KIND,
                key,
                symbol,
                "finnhub",
                preserved,
                now=now,
                event_time=matched["event_time"],
                available_at=matched["available_at"],
            )
            return
        aliases = set(previous.get("aliases", [])) | {alias}
        dates = list(previous.get("date_history", []))
        if previous.get("date") and (not dates or dates[-1] != previous["date"]):
            dates.append(previous["date"])
        if not dates or dates[-1] != item["date"]:
            dates.append(item["date"])
        payload = dict(
            item,
            aliases=sorted(aliases),
            date_history=dates[-16:],
            checked_at=item.get("checked_at", now),
        )
        payload["revision"] = previous.get("revision", 0) + int(
            bool(previous.get("date") and previous["date"] != item["date"])
        )
        revisions = list(previous.get("revisions", []))
        if previous.get("date") and previous["date"] != item["date"]:
            revisions.append({"from": previous["date"], "to": item["date"], "observed_at": now})
        payload["revisions"] = revisions[-16:]
        related_dates = set(previous.get("related_dates", []))
        payload.pop("alias", None)
        conflicts = []
        for other in records:
            old = other["payload"]
            if other["key"] == key or old.get("superseded_by"):
                continue
            if old.get("date") == item["date"] and other["source"] != source:
                aliases.update(old.get("aliases", []))
                self.signals.put(
                    KIND,
                    other["key"],
                    symbol,
                    other["source"],
                    dict(old, superseded_by=key),
                    now=now,
                    event_time=other["event_time"],
                    available_at=other["available_at"],
                )
                continue
            if other["source"] == source or not old.get("date"):
                continue
            if abs((_day(old["date"]) - _day(item["date"])).days) > 45:
                continue
            conflicts.append({"source": other["source"], "date": old["date"]})
            related_dates.add(old["date"])
            if source == "finnhub":
                self.signals.put(
                    KIND,
                    other["key"],
                    symbol,
                    other["source"],
                    dict(old, conflict=True, conflict_with=key),
                    now=now,
                    event_time=other["event_time"],
                    available_at=other["available_at"],
                )
            else:
                payload["conflict"] = True
                self.signals.put(
                    KIND,
                    other["key"],
                    symbol,
                    other["source"],
                    dict(old, source_conflicts=[{"source": source, "date": item["date"]}]),
                    now=now,
                    event_time=other["event_time"],
                    available_at=other["available_at"],
                )
        payload["aliases"] = sorted(aliases)
        if conflicts:
            payload["source_conflicts"] = conflicts
        payload["related_dates"] = sorted(related_dates)[-16:]
        self.results.track(previous, payload, now)
        event_time = datetime.combine(_day(item["date"]), datetime.min.time(), NY).timestamp()
        self.signals.put(
            KIND,
            key,
            symbol,
            source,
            payload,
            now=now,
            event_time=event_time,
            available_at=matched["available_at"] if matched else now,
        )

    def _primary(self, symbol, today, now):
        raw = self.http.finnhub(
            "calendar/earnings",
            symbol=symbol,
            **{
                "from": (today - timedelta(days=7)).isoformat(),
                "to": (today + timedelta(days=90)).isoformat(),
            },
        )
        if not isinstance(raw, dict) or not isinstance(raw.get("earningsCalendar"), list):
            raise ApiError("Finnhub earnings response is unavailable or invalid.")
        result = []
        for row in raw["earningsCalendar"]:
            if not isinstance(row, dict) or row.get("symbol") != symbol:
                continue
            try:
                day = _day(row.get("date"))
                if not today - timedelta(days=7) <= day <= today + timedelta(days=90):
                    continue
                year, quarter = row.get("year"), row.get("quarter")
                if (
                    type(year) is int
                    and 1900 <= year <= 2200
                    and type(quarter) is int
                    and 1 <= quarter <= 4
                ):
                    alias = f"{symbol}:finnhub:{year}:Q{quarter}"
                    period = f"FY{year} Q{quarter}"
                else:
                    alias, period = f"{symbol}:finnhub:date:{day}", "fiscal period unknown"
                figures = {
                    field: number(row.get(field), revenue=field.startswith("revenue"))
                    for field in ("epsEstimate", "epsActual", "revenueEstimate", "revenueActual")
                }
                result.append(
                    {
                        "alias": alias,
                        "date": day.isoformat(),
                        "period": period,
                        "hour": _hour(row.get("hour")),
                        "certainty": "provider-reported",
                        "reported": any(
                            figures[field] is not None for field in ("epsActual", "revenueActual")
                        ),
                        **figures,
                        "source_url": SOURCES["finnhub"],
                    }
                )
            except (ValueError, TypeError):
                continue
        return result

    def _fallback(self, symbols, today, now):
        from trabe.ledger import Ledger

        covered = Ledger(self.store).coverage() | set(symbols)
        cached = self.signals.get("earnings-source", "alpha-calendar")
        fresh = bool(cached and 0 <= now - cached["payload"].get("checked_at", 0) < DAY)
        cached_symbols = set(cached["payload"].get("symbols", [])) if cached else set()
        expanding = fresh and not covered <= cached_symbols
        if fresh and not expanding:
            return {
                symbol: (self.signals.get("earnings-source", "alpha:" + symbol) or {})
                .get("payload", {})
                .get("rows", [])
                for symbol in cached["payload"].get("symbols", [])
            }
        if not expanding and not self.signals.due(KIND, "alphavantage", "*", DAY, now):
            if cached:
                return {
                    symbol: (self.signals.get("earnings-source", "alpha:" + symbol) or {})
                    .get("payload", {})
                    .get("rows", [])
                    for symbol in cached["payload"].get("symbols", [])
                }
            raise ApiError("Alpha Vantage earnings fallback is waiting for retry.")
        self.signals.attempt(KIND, "alphavantage", "*", now)
        try:
            raw = self.http.alpha_csv("EARNINGS_CALENDAR", horizon="3month")
            received_at = self._received(now)
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8-sig")
            if not isinstance(raw, str) or len(raw) > 8 * 1024**2 or raw.lstrip().startswith("{"):
                raise ApiError("Alpha Vantage earnings calendar is unavailable.")
            reader = csv.DictReader(io.StringIO(raw.lstrip("\ufeff")))
            if not reader.fieldnames or not {"symbol", "reportDate"} <= set(reader.fieldnames):
                raise ApiError("Alpha Vantage earnings calendar format is unavailable.")
            # alpha_csv shares the full daily response cache. A new symbol can
            # therefore be parsed immediately without another provider request.
            checked_at = cached["payload"]["checked_at"] if expanding else received_at
            if expanding:
                covered |= cached_symbols
            calendar = {}
            for row in reader:
                symbol = row.get("symbol")
                if symbol not in covered:
                    continue
                try:
                    day = _day(row.get("reportDate"))
                    if not today <= day <= today + timedelta(days=90):
                        continue
                    fiscal = row.get("fiscalDateEnding", "")
                    try:
                        period = "period ending " + _day(fiscal).isoformat()
                    except ValueError:
                        fiscal, period = "date:" + day.isoformat(), "fiscal period unknown"
                    calendar.setdefault(symbol, []).append(
                        {
                            "alias": f"{symbol}:alphavantage:{fiscal}",
                            "date": day.isoformat(),
                            "period": period,
                            "hour": _hour(row.get("timeOfTheDay")),
                            "certainty": "provider-reported",
                            "reported": False,
                            "epsEstimate": number(row.get("estimate")),
                            "fiscal_end": fiscal if not fiscal.startswith("date:") else None,
                            "checked_at": checked_at,
                            "source_url": SOURCES["alphavantage"],
                        }
                    )
                except (ValueError, TypeError):
                    continue
            for symbol in covered:
                self.signals.put(
                    "earnings-source",
                    "alpha:" + symbol,
                    symbol,
                    "alphavantage",
                    {"checked_at": checked_at, "rows": calendar.get(symbol, [])},
                    now=received_at,
                )
            self.signals.put(
                "earnings-source",
                "alpha-calendar",
                "*",
                "alphavantage",
                {"checked_at": checked_at, "symbols": sorted(covered)},
                now=received_at,
            )
            self.signals.ok(KIND, "alphavantage", "*", received_at)
            return calendar
        except WorkStopped:
            raise
        except Exception:
            self.signals.fail(KIND, "alphavantage", "*", "Calendar unavailable", now)
            raise ApiError("Alpha Vantage earnings fallback is unavailable.") from None

    def _refresh_interval(self, symbol, now):
        for row in self._records([symbol]):
            payload = row["payload"]
            if payload.get("superseded_by") or not result_window(payload, now):
                continue
            if any(
                not comparison(
                    payload.get("comparisons", {}).get(metric, {}).get("actual"),
                    payload.get("comparisons", {}).get(metric, {}).get("estimate"),
                )
                for metric in ("eps", "revenue")
            ):
                elapsed = (datetime.fromtimestamp(now, NY).date() - _day(payload["date"])).days
                return RESULT_INTERVAL if elapsed <= 2 else DELAYED_RESULT_INTERVAL
        return DAY

    def refresh(self, symbols, *, now, force=False):
        symbols = list(dict.fromkeys(symbols))
        today = datetime.fromtimestamp(now, NY).date()
        for symbol in symbols:
            interval = self._refresh_interval(symbol, now)
            if force or self.signals.due(KIND, "finnhub", symbol, interval, now):
                self.signals.attempt(KIND, "finnhub", symbol, now)
                rows = []
                try:
                    rows = self._primary(symbol, today, now)
                    received_at = self._received(now)
                    for item in rows:
                        self._store_event(symbol, "finnhub", item, received_at)
                    self.signals.ok(KIND, "finnhub", symbol, received_at)
                    self._status(
                        symbol,
                        "Finnhub checked; "
                        + ("schedule cached" if rows else "no upcoming report returned"),
                    )
                except WorkStopped:
                    raise
                except Exception:
                    self.signals.fail(
                        KIND,
                        "finnhub",
                        symbol,
                        "Calendar unavailable",
                        now,
                        retry=min(interval, 3600),
                    )
                    self._status(symbol, "Finnhub unavailable; retaining cached schedule")
                if not rows:
                    try:
                        calendar = self._fallback(symbols, today, now)
                        received_at = self._received(now)
                        for item in calendar.get(symbol, []):
                            self._store_event(symbol, "alphavantage", item, received_at)
                        self.signals.ok(KIND, "alphavantage", symbol, received_at)
                        self._status(
                            symbol, "Alpha Vantage fallback checked; provider-reported schedule"
                        )
                    except WorkStopped:
                        raise
                    except Exception:
                        self.signals.fail(KIND, "alphavantage", symbol, "Fallback unavailable", now)
                        self._status(symbol, "Sources unavailable; cached schedule may be stale")
            self.results.refresh(
                symbol, now=now, interval=self._refresh_interval(symbol, now), force=force
            )
            self.results.attach(symbol, now=self._received(now))
            self.results.fallback(symbol, now=self._received(now), force=force)
            from trabe.earnings_news import EarningsNews

            EarningsNews(self.store, self.http).refresh(
                symbol, now=self._received(now), force=force
            )

    def _upcoming(self, symbols, now):
        today = datetime.fromtimestamp(now, NY).date()
        rows = []
        for row in self._records(symbols):
            payload = row["payload"]
            if payload.get("superseded_by") or payload.get("reported"):
                continue
            try:
                day = _day(payload["date"])
                if not today <= day <= today + timedelta(days=90):
                    continue
                if day == today and payload.get("hour") in ("before-open", "during-session"):
                    try:
                        session = USCalendar().session(today)
                    except ValueError:
                        session = None
                    edge = (
                        session.opens
                        if session and payload["hour"] == "before-open"
                        else session.closes
                        if session
                        else None
                    )
                    if edge is not None and now >= edge:
                        continue
            except (KeyError, ValueError):
                continue
            rows.append(row)
        return sorted(rows, key=lambda r: (r["payload"]["date"], r["symbol"], r["key"]))

    def _exposure(self, symbol, now):
        try:
            currency, _, rows = Portfolio(self.store).calculate(symbol, now)
            if not rows or rows[0]["quantity"] <= 0:
                return "Watchlist only"
            row = rows[0]
            text = f"Held: {row['quantity']} shares"
            if row["value"] is None:
                return text + " • exposure unavailable (price/FX missing)"
            quote = row["quote"] or {}
            age = now - quote.get("source_time", 0)
            stale = age > int(self.store.settings()["max_source_age_seconds"])
            return (
                text
                + f" • observed exposure {row['value']:.2f} {currency}"
                + (" (stale price)" if stale else "")
            )
        except (ValueError, KeyError, ArithmeticError):
            return "Exposure unavailable"

    @staticmethod
    def _after_close_unconfirmed(item, now):
        today = datetime.fromtimestamp(now, NY).date()
        if item.get("hour") != "after-close" or item.get("date") != today.isoformat():
            return False
        try:
            session = USCalendar().session(today)
        except ValueError:
            return False
        return bool(session and now >= session.closes and not item.get("reported"))

    def _time_label(self, item, now):
        if self._after_close_unconfirmed(item, now):
            return "after-close • release status unconfirmed"
        return item["hour"]

    @staticmethod
    def _countdown(item, now):
        days = (_day(item["date"]) - datetime.fromtimestamp(now, NY).date()).days
        if days < 0:
            return f"{-days} day{'s' if days != -1 else ''} ago"
        return "today" if days == 0 else "tomorrow" if days == 1 else f"in {days} days"

    def _schedule(self, row, now):
        item = row["payload"]
        return (
            f"{row['symbol']} • {item['date']} • {self._countdown(item, now)}\n"
            f"{self._time_label(item, now)} • {item['period']}"
        )

    def _recent_results(self, symbols, now):
        today = datetime.fromtimestamp(now, NY).date()
        return sorted(
            (
                row
                for row in self._records(symbols)
                if not row["payload"].get("superseded_by")
                and row["payload"].get("comparisons")
                and -1 <= (today - _day(row["payload"]["date"])).days <= 7
            ),
            key=lambda row: row["payload"]["date"],
            reverse=True,
        )

    def _waiting_results(self, symbols, now):
        today = datetime.fromtimestamp(now, NY).date()
        return [
            row
            for row in self._records(symbols)
            if not row["payload"].get("superseded_by")
            and not row["payload"].get("reported")
            and 0 <= (today - _day(row["payload"]["date"])).days <= 7
            and (today > _day(row["payload"]["date"]) or result_window(row["payload"], now))
        ]

    @staticmethod
    def _result_card(row):
        payload = row["payload"]
        lines = [f"{row['symbol']} • {payload['period']} • reported results"]
        news_eps = payload["comparisons"].get("eps", {}).get("source") == "yahoo-news"
        for metric in ("eps", "revenue"):
            data = payload["comparisons"].get(metric, {})
            if news_eps and metric == "revenue" and data.get("actual") is None:
                lines.append("Revenue actual: unavailable")
            else:
                lines.append(metric_line(metric, data))
        if payload["comparisons"].get("eps", {}).get("source") == "alphavantage":
            lines.append("Alpha Vantage fallback • EPS uses its own estimate.")
        if payload["comparisons"].get("revenue", {}).get("source") == "alphavantage":
            lines.append(
                "Revenue: Alpha Vantage fallback • no paired estimate; no beat/miss grade."
            )
        if news_eps:
            saved = calendar_estimates(payload, row.get("source"))
            if saved:
                lines.append(saved)
            lines.append(
                "Yahoo news • current fiscal quarter matched; estimate basis not matched. No beat/miss grade."
            )
        return "\n".join(lines)

    def report(self, symbols, *, now):
        rows = self._upcoming(symbols, now)
        recent = self._recent_results(symbols, now)
        waiting = [
            row
            for row in self._waiting_results(symbols, now)
            if row["key"] not in {r["key"] for r in rows}
        ]
        result = ["📅 Earnings"]
        if not rows and not recent and not waiting:
            result.append(
                "No upcoming report cached. Coverage may be unavailable; use /earnings refresh."
            )
        for row in rows[:10]:
            item = row["payload"]
            checked = datetime.fromtimestamp(
                item.get("checked_at", row["observed_at"]), UTC
            ).strftime("%d %b %H:%M UTC")
            stale = now - item.get("checked_at", 0) > 2 * DAY
            result.append("\n" + self._schedule(row, now))
            result.append(expectations(item))
            if item.get("conflict") or item.get("source_conflicts"):
                result.append(
                    "  Source conflict: "
                    + ", ".join(
                        f"{claim['source']} {claim['date']}"
                        for claim in item.get("source_conflicts", [])
                    )
                    + "; identity/date remains uncertain."
                )
            exposure = self._exposure(row["symbol"], now)
            if exposure != "Watchlist only":
                result.append(exposure)
            result.append(f"Last checked: {checked}" + (" • STALE" if stale else ""))
        if len(rows) > 10:
            result.append(f"Showing first 10 of {len(rows)} cached upcoming reports.")
        for row in waiting[:3]:
            result.extend(
                (
                    "\n" + self._schedule(row, now),
                    expectations(row["payload"]),
                    "Results not confirmed in available provider data; refresh or check the source.",
                )
            )
        for row in recent[:3]:
            result.append("\n" + self._result_card(row))
        result.append("\nRecent quarters • EPS (actual vs expected)")
        for symbol in list(dict.fromkeys(symbols))[:10]:
            history = self.results.history(symbol)
            if not history:
                result.append(f"{symbol}: quarterly history unavailable.")
            elif len(symbols) == 1:
                for quarter in history:
                    result.append(
                        f"FY{quarter['year']} Q{quarter['quarter']} • "
                        + metric_line("eps", quarter)
                    )
            else:
                result.append(
                    symbol
                    + ": "
                    + " · ".join(
                        f"FY{q['year']} Q{q['quarter']} {comparison(q['actual'], q['estimate'])[0] if comparison(q['actual'], q['estimate']) else 'unavailable'}"
                        for q in history
                    )
                )
            history_status = next(
                (
                    status
                    for status in self.signals.status(KIND)
                    if status["symbol"] == symbol and status["source"] == "finnhub-history"
                ),
                None,
            )
            cached_history = self.signals.get("earnings-history", symbol)
            if history and (
                (history_status and history_status["error"])
                or (cached_history and now - cached_history["payload"]["checked_at"] > 2 * DAY)
            ):
                result.append(
                    f"⚠️ {symbol}: quarterly history may be stale; cached results retained."
                )
        for symbol in list(dict.fromkeys(symbols))[:8]:
            for status in self.signals.status(KIND):
                if (
                    status["symbol"] == symbol
                    and (
                        status["source"] == "yahoo-news"
                        or (
                            status["source"].startswith("alphavantage-")
                            and self.store.state("earnings_alpha_disabled", "1") == "0"
                        )
                    )
                    and status["error"]
                ):
                    result.append(f"⚠️ {symbol}: {status['error']}")
            state = self.store.state("earnings_status_" + symbol)
            if state and state != "Finnhub checked; schedule cached":
                result.append(f"{symbol}: {state}")
        result.append(
            "\nDates may change. These are provider-reported schedules; times are approximate. EPS/revenue use provider units, without currency conversion."
        )
        return "\n".join(result)

    def _last_notice(self, symbol, dates):
        # Read the shared claim ledger in delivery order: a date can move away
        # and then return, so membership in a set of old dates is insufficient.
        with self.store.connection() as db:
            notices = db.execute(
                "SELECT event_key FROM signal_notifications WHERE kind=? AND "
                "(event_key LIKE ? OR event_key LIKE ? OR event_key LIKE ?) "
                "ORDER BY created_at DESC,rowid DESC",
                (KIND, f"warning:{symbol}:%", f"revision:{symbol}:%", f"conflict:{symbol}:%"),
            )
            for (key,) in notices:
                target = key.rsplit(":", 1)[-1]
                if target in dates:
                    return target
        return None

    def alerts(self, symbols, *, now):
        today = datetime.fromtimestamp(now, NY).date()
        lead = int(self.store.settings().get("earnings_warning_days", "5"))
        result = []
        for row in self._upcoming(symbols, now):
            item, symbol = row["payload"], row["symbol"]
            if item.get("conflict") or now - item.get("checked_at", 0) > 2 * DAY:
                continue
            days = (_day(item["date"]) - today).days
            history = list(item.get("date_history", []))
            if not history or history[-1] != item["date"]:
                history.append(item["date"])
            related = item.get("related_dates", [])
            noticed = self._last_notice(symbol, set(history) | set(related))
            if noticed and noticed != item["date"] and noticed not in history:
                key = f"conflict:{symbol}:{noticed}:{item['date']}"
                text = (
                    f"📅 Earnings source disagreement • {symbol}\n"
                    f"Current {row['source']} schedule: {item['date']}; previously warned schedule: {noticed}.\n"
                    "The sources disagree; this is not a confirmed date revision.\n"
                )
            elif noticed and noticed != item["date"]:
                key = f"revision:{row['key']}:{item.get('revision', 0)}:{noticed}:{item['date']}"
                text = f"📅 Earnings date changed • {symbol}\nExpected date changed: {noticed} → {item['date']}\n"
            elif noticed or days > lead or self._after_close_unconfirmed(item, now):
                continue
            else:
                key = f"warning:{symbol}:{item['date']}"
                text = f"📅 Upcoming earnings • {symbol}\nExpected: {item['date']} • {self._countdown(item, now)} • {self._time_label(item, now)}\n"
            text += item["period"] + "\n"
            text += expectations(item) + "\n"
            exposure = self._exposure(symbol, now)
            if exposure != "Watchlist only":
                text += exposure + "\n"
            text += (
                f"Last checked: {datetime.fromtimestamp(item['checked_at'], UTC).strftime('%d %b %H:%M UTC')}\n"
                "Dates may change. Provider-reported schedule."
            )
            result.append((key, text))
        result.extend(self.results.alerts(symbols, now=now))
        return result

    def briefing(self, symbols, *, now, limit=3):
        results = [self._result_card(row) for row in self._recent_results(symbols, now)[:limit]]
        upcoming = self._upcoming(symbols, now)
        waiting = [
            row
            for row in self._waiting_results(symbols, now)
            if row["key"] not in {r["key"] for r in upcoming}
        ]
        results.extend(
            (
                self._schedule(row, now)
                + "\n"
                + expectations(row["payload"])
                + "\nResults not confirmed in available provider data; refresh or check the source."
            )
            for row in waiting[: max(0, limit - len(results))]
        )
        return results + [
            self._schedule(row, now)
            + "\n"
            + expectations(row["payload"])
            + (
                " • source conflict"
                if row["payload"].get("conflict") or row["payload"].get("source_conflicts")
                else ""
            )
            + (" • stale" if now - row["payload"].get("checked_at", 0) > 2 * DAY else "")
            for row in upcoming[: max(0, limit - len(results))]
        ]
