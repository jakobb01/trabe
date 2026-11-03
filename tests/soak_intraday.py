"""Offline multi-session storage/ingestion soak using the production code."""

import argparse
import json
import resource
import tempfile
from datetime import date, timedelta
from pathlib import Path

from trabe.calendar import USCalendar
from trabe.maintenance import Maintenance
from trabe.monitor import Ingestion
from trabe.store import Store


def soak(symbols=50, sessions=8):
    with tempfile.TemporaryDirectory() as directory:
        store = Store(Path(directory) / "soak.db")
        store.set_setting("market_provider", "finnhub")
        store.set_setting("monitoring", "on")
        stocks = [f"T{n:02d}" for n in range(symbols)]
        for ticker in stocks:
            store.add(ticker, 2)
        engine = Ingestion(store, 123)
        engine.configure(store.settings(), {}, stocks)
        maintenance = Maintenance(store)
        calendar = USCalendar()
        day = date(2026, 9, 1)
        sizes, peaks = [], []
        for _ in range(sessions):
            while not (session := calendar.session(day)):
                day += timedelta(days=1)
            for ticker in stocks:
                store.save_baseline(
                    ticker,
                    session.day,
                    {
                        "previous_date": calendar.previous(day).day,
                        "previous_close": 100,
                        "open_price": 100,
                        "source_time": session.opens,
                        "accepted": True,
                        "reason": "",
                    },
                )
                engine.register(ticker, session, store.baseline(ticker, session.day), session.opens)
            for minute in range(session.minutes):
                stamp = session.opens + minute * 60 + 1
                for ticker in stocks:
                    assert engine.observe(
                        {"s": ticker, "p": 100 + minute / 1000, "t": int(stamp * 1000)}, now=stamp
                    )
                engine.flush(now=stamp + 60)
                assert len(engine.buckets) <= symbols
            values = maintenance.metrics()
            assert values["minute_rows"] == symbols * session.minutes
            peaks.append(
                sum(values[key] for key in ("database_bytes", "wal_bytes", "shared_memory_bytes"))
            )
            maintenance.run_once(now=session.closes + 601)
            values = maintenance.metrics()
            peaks.append(
                sum(values[key] for key in ("database_bytes", "wal_bytes", "shared_memory_bytes"))
            )
            assert values["minute_rows"] == 0
            sizes.append(values["database_bytes"])
            day += timedelta(days=1)
        retained = maintenance.metrics()["daily_rows"]
        assert retained == symbols * sessions
        # Daily rows may grow; minute storage reuses existing pages across sessions.
        assert sizes[-1] - sizes[0] < 1024**2
        maintenance.run_once(now=calendar.session(date(2027, 1, 4)).opens)
        assert maintenance.metrics()["daily_rows"] == 0
        memory = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024
        assert memory < 256 * 1024**2
        return {
            "symbols": symbols,
            "sessions": sessions,
            "observations": engine.received,
            "daily_rows_before_expiry": retained,
            "daily_rows_after_expiry": 0,
            "database_bytes_by_session": sizes,
            "peak_database_wal_shm_bytes": max(peaks),
            "max_rss_bytes": memory,
        }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbols", type=int, default=50)
    parser.add_argument("--sessions", type=int, default=8)
    args = parser.parse_args()
    print(json.dumps(soak(args.symbols, args.sessions), indent=2))
