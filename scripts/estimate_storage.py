#!/usr/bin/env python3
"""Measure a proposed 0.3 schema with synthetic data in temporary SQLite files.

This is a sizing experiment, not an application migration or a Pi benchmark.
No credentials, real prices, application database or network access are used.
"""

import argparse
import json
import sqlite3
import tempfile
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

SCHEMA = """
CREATE TABLE instruments (
    id INTEGER PRIMARY KEY, symbol TEXT NOT NULL UNIQUE, currency TEXT NOT NULL
);
CREATE TABLE feeds (
    id INTEGER PRIMARY KEY, provider TEXT NOT NULL, feed TEXT NOT NULL
);
CREATE TABLE minute_observations (
    instrument_id INTEGER NOT NULL, feed_id INTEGER NOT NULL,
    session_date TEXT NOT NULL, minute_start_ms INTEGER NOT NULL,
    observed_open REAL NOT NULL, observed_high REAL NOT NULL,
    observed_low REAL NOT NULL, observed_last REAL NOT NULL,
    first_source_ms INTEGER NOT NULL, last_source_ms INTEGER NOT NULL,
    tick_count INTEGER NOT NULL, quality_flags INTEGER NOT NULL,
    PRIMARY KEY (session_date, instrument_id, feed_id, minute_start_ms)
) WITHOUT ROWID;
CREATE TABLE daily_summaries (
    instrument_id INTEGER NOT NULL, feed_id INTEGER NOT NULL,
    session_date TEXT NOT NULL,
    observed_open REAL NOT NULL, observed_high REAL NOT NULL,
    observed_low REAL NOT NULL, observed_last REAL NOT NULL,
    previous_close REAL NOT NULL,
    first_source_ms INTEGER NOT NULL, last_source_ms INTEGER NOT NULL,
    observed_minutes INTEGER NOT NULL, expected_minutes INTEGER NOT NULL,
    max_gap_seconds INTEGER NOT NULL, alert_count INTEGER NOT NULL,
    quality_flags INTEGER NOT NULL,
    PRIMARY KEY (instrument_id, feed_id, session_date)
) WITHOUT ROWID;
CREATE INDEX summaries_by_date ON daily_summaries(session_date);
"""


def session_start_ms(session: date) -> int:
    opening = datetime.combine(session, time(9, 30), ZoneInfo("America/New_York"))
    return int(opening.timestamp() * 1000)


def summary(instrument: int, session: date) -> tuple:
    # Fractional values avoid SQLite's space optimization for integer-valued REALs.
    price = 100.123456 + instrument * 2.78654
    source_ms = session_start_ms(session)
    return (
        instrument,
        1,
        session.isoformat(),
        price,
        price + 0.53127,
        price - 0.37281,
        price + 0.21493,
        price - 0.02937,
        source_ms,
        source_ms + 390 * 60_000 - 1,
        390,
        390,
        60,
        0,
        0,
    )


def fill_minutes(db: sqlite3.Connection, instruments: int, session: date, observe):
    first_ms = session_start_ms(session)
    for minute in range(390):
        rows = []
        for instrument in range(1, instruments + 1):
            price = 100.123456 + instrument * 2.78654 + minute * 0.00073
            source_ms = first_ms + minute * 60_000
            rows.append(
                (
                    instrument,
                    1,
                    session.isoformat(),
                    source_ms,
                    price,
                    price + 0.05312,
                    price - 0.03728,
                    price + 0.02149,
                    source_ms,
                    source_ms + 59_000,
                    173,
                    0,
                )
            )
        # One transaction per minute, rather than a commit for every trade.
        with db:
            db.executemany("INSERT INTO minute_observations VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        observe()


def measure(instruments: int, retained_sessions: int) -> dict:
    with tempfile.TemporaryDirectory(prefix="trabe-storage-estimate-") as directory:
        path = Path(directory) / "market.sqlite3"
        db = sqlite3.connect(path)
        try:
            db.execute("PRAGMA page_size=4096")
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=FULL")
            db.execute("PRAGMA wal_autocheckpoint=1000")
            db.executescript(SCHEMA)
            with db:
                db.execute("INSERT INTO feeds VALUES (1,'finnhub','us')")
                db.executemany(
                    "INSERT INTO instruments VALUES (?,?,'USD')",
                    ((i, f"SYM{i:04d}") for i in range(1, instruments + 1)),
                )
                session = date(2026, 11, 3)
                # 65 past weekdays plus today's rollup form a 66-session archive.
                past = session
                for _ in range(retained_sessions - 1):
                    past -= timedelta(days=1)
                    while past.weekday() >= 5:
                        past -= timedelta(days=1)
                    db.executemany(
                        "INSERT INTO daily_summaries VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (summary(i, past) for i in range(1, instruments + 1)),
                    )
            db.execute("PRAGMA wal_checkpoint(TRUNCATE)")

            peak = 0
            peak_wal = 0

            def observe():
                nonlocal peak, peak_wal
                sizes = {
                    suffix: Path(str(path) + suffix).stat().st_size
                    for suffix in ("", "-wal", "-shm")
                    if Path(str(path) + suffix).exists()
                }
                peak = max(peak, sum(sizes.values()))
                peak_wal = max(peak_wal, sizes.get("-wal", 0))

            fill_minutes(db, instruments, session, observe)
            active_bytes = db.execute("PRAGMA page_count").fetchone()[0] * 4096
            active_rows = db.execute("SELECT COUNT(*) FROM minute_observations").fetchone()[0]

            # Model a transactional end-of-session rollup and minute-row deletion.
            with db:
                for instrument in range(1, instruments + 1):
                    rows = db.execute(
                        "SELECT observed_open,observed_high,observed_low,observed_last,"
                        "first_source_ms,last_source_ms FROM minute_observations "
                        "WHERE session_date=? AND instrument_id=? AND feed_id=1 "
                        "ORDER BY minute_start_ms",
                        (session.isoformat(), instrument),
                    ).fetchall()
                    values = list(summary(instrument, session))
                    values[3:7] = [
                        rows[0][0],
                        max(row[1] for row in rows),
                        min(row[2] for row in rows),
                        rows[-1][3],
                    ]
                    values[8:11] = [rows[0][4], rows[-1][5], len(rows)]
                    db.execute(
                        "INSERT INTO daily_summaries VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        values,
                    )
                db.execute("DELETE FROM minute_observations WHERE session_date=?", (str(session),))
            observe()
            db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            after_rollup_bytes = path.stat().st_size
            free_pages = db.execute("PRAGMA freelist_count").fetchone()[0]
            archive_rows = db.execute("SELECT COUNT(*) FROM daily_summaries").fetchone()[0]
            assert active_rows == instruments * 390
            assert archive_rows == instruments * retained_sessions
            assert db.execute("SELECT COUNT(*) FROM minute_observations").fetchone()[0] == 0

            # A packed copy quantifies live archive size; nightly VACUUM is not proposed.
            packed = Path(directory) / "packed.sqlite3"
            db.execute("VACUUM INTO ?", (str(packed),))
            packed_archive_bytes = packed.stat().st_size

            # Demonstrate that tomorrow can reuse deleted pages without daily growth.
            fill_minutes(db, instruments, session + timedelta(days=1), observe)
            db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            return {
                "symbols": instruments,
                "minute_rows_per_full_session": active_rows,
                "retained_daily_sessions": retained_sessions,
                "daily_summary_rows": archive_rows,
                "active_database_bytes": active_bytes,
                "archive_only_packed_bytes": packed_archive_bytes,
                "allocated_after_rollup_bytes": after_rollup_bytes,
                "reusable_bytes_after_rollup": free_pages * 4096,
                "next_session_database_bytes": path.stat().st_size,
                "peak_wal_bytes": peak_wal,
                "peak_database_wal_shm_bytes": peak,
                "three_retained_backup_copies_bytes": after_rollup_bytes * 3,
                "one_extra_in_progress_backup_bytes": after_rollup_bytes,
            }
        finally:
            db.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbols", nargs="+", type=int, default=[10, 50])
    parser.add_argument("--sessions", type=int, default=66)
    args = parser.parse_args()
    if args.sessions < 1 or any(number < 1 for number in args.symbols):
        parser.error("Symbols and retained sessions must be positive.")
    print(
        json.dumps(
            {
                "sqlite_version": sqlite3.sqlite_version,
                "page_size": 4096,
                "scope": "Synthetic proposed market-data tables; excludes existing application state",
                "scenarios": [measure(number, args.sessions) for number in args.symbols],
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
