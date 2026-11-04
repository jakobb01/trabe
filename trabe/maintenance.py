"""Crash-safe daily rollups, calendar-month expiry and bounded backup storage."""

import fcntl
import json
import os
import re
import shutil
import time
from datetime import datetime, timedelta
from pathlib import Path

from trabe.calendar import NY, retention_cutoff

AUTO_BACKUP = re.compile(r"pre-deploy-\d{8}T\d{6}Z-[a-zA-Z0-9-]+\.sqlite3\Z")


def release_backup(directory: Path, name: str):
    if not AUTO_BACKUP.fullmatch(name):
        raise ValueError("Invalid automatic backup name.")
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(directory / ".backup.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        marker = directory / ".protected.json"
        if marker.exists() and json.loads(marker.read_text()).get("name") == name:
            marker.unlink()
    finally:
        os.close(descriptor)


def prune_backups(directory: Path, *, now: float | None = None, keep: int = 3) -> dict:
    now = time.time() if now is None else now
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(directory / ".backup.lock", os.O_CREAT | os.O_RDWR, 0o600)
    removed = []
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"removed": [], "busy": True}
        marker = directory / ".protected.json"
        protected = json.loads(marker.read_text()).get("name") if marker.exists() else None
        cutoff = retention_cutoff(datetime.fromtimestamp(now, NY).date()).strftime("%Y%m%d")
        candidates = sorted(
            (
                path
                for path in directory.iterdir()
                if path.is_file()
                and not path.is_symlink()
                and AUTO_BACKUP.fullmatch(path.name)
                and path.name != protected
            ),
            key=lambda path: path.name,
            reverse=True,
        )
        for index, path in enumerate(candidates):
            if index >= keep or path.name[11:19] <= cutoff:
                path.unlink()
                removed.append(path.name)
        return {"removed": removed, "busy": False, "protected": protected}
    finally:
        os.close(descriptor)


class Maintenance:
    def __init__(self, store, owner_id: int | None = None):
        self.store, self.owner_id = store, owner_id
        self.last_run = 0.0
        self.backups = store.path.parent / "backups"

    def rollup(self, day: str, *, now: float) -> bool:
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            session = db.execute(
                "SELECT opens,closes,finalized,quality_flags FROM market_sessions WHERE session_date=?",
                (day,),
            ).fetchone()
            if not session or session[2] or now < session[1] + 600:
                return False
            expected = int((session[1] - session[0]) / 60)
            symbols = list(
                db.execute(
                    "SELECT s.instrument_id,s.feed_id,i.symbol,f.provider,s.started_at "
                    "FROM session_symbols s JOIN instruments i ON i.id=s.instrument_id "
                    "JOIN feeds f ON f.id=s.feed_id WHERE session_date=?",
                    (day,),
                )
            )
            for instrument, feed, ticker, provider, started_at in symbols:
                rows = list(
                    db.execute(
                        "SELECT observed_open,observed_high,observed_low,observed_last,first_source_ms,last_source_ms,quality_flags "
                        "FROM minute_observations WHERE session_date=? AND instrument_id=? AND feed_id=? ORDER BY minute_start_ms",
                        (day, instrument, feed),
                    )
                )
                baseline = db.execute(
                    "SELECT previous_close,previous_date,source_time,accepted FROM baselines "
                    "WHERE ticker=? AND provider=? AND session_date=?",
                    (ticker, provider, day),
                ).fetchone()
                alerts = db.execute(
                    "SELECT COUNT(*) FROM alert_events WHERE ticker=? AND session_date=?",
                    (ticker, day),
                ).fetchone()[0]
                flags = session[3]
                if rows:
                    first, last = rows[0][4], rows[-1][5]
                    prices = (
                        rows[0][0],
                        max(row[1] for row in rows),
                        min(row[2] for row in rows),
                        rows[-1][3],
                    )
                    gaps = [first / 1000 - session[0], session[1] - last / 1000]
                    gaps.extend(
                        (after[4] - before[5]) / 1000 for before, after in zip(rows, rows[1:])
                    )
                    max_gap = max(0, int(max(gaps)))
                    for row in rows:
                        flags |= row[6]
                    if len(rows) < expected or max_gap > 120 or started_at > session[0] + 120:
                        flags |= 1
                else:
                    prices, first, last, max_gap = (
                        (None, None, None, None),
                        None,
                        None,
                        expected * 60,
                    )
                    flags |= 2
                if not baseline or not baseline[3]:
                    flags |= 4
                db.execute(
                    """INSERT INTO daily_summaries VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(instrument_id,feed_id,session_date) DO UPDATE SET
                    observed_open=excluded.observed_open,observed_high=excluded.observed_high,
                    observed_low=excluded.observed_low,observed_last=excluded.observed_last,
                    previous_close=excluded.previous_close,previous_date=excluded.previous_date,
                    baseline_source_time=excluded.baseline_source_time,first_source_ms=excluded.first_source_ms,
                    last_source_ms=excluded.last_source_ms,observed_minutes=excluded.observed_minutes,
                    expected_minutes=excluded.expected_minutes,max_gap_seconds=excluded.max_gap_seconds,
                    alert_count=excluded.alert_count,quality_flags=excluded.quality_flags""",
                    (
                        instrument,
                        feed,
                        day,
                        *prices,
                        baseline[0] if baseline else None,
                        baseline[1] if baseline else None,
                        baseline[2] if baseline else None,
                        first,
                        last,
                        len(rows),
                        expected,
                        max_gap,
                        alerts,
                        flags,
                    ),
                )
            db.execute("DELETE FROM minute_observations WHERE session_date=?", (day,))
            db.execute("UPDATE market_sessions SET finalized=1 WHERE session_date=?", (day,))
            db.execute("INSERT OR REPLACE INTO state VALUES ('last_rollup',?)", (day,))
        return True

    def expire(self, now: float):
        cutoff = retention_cutoff(datetime.fromtimestamp(now, NY).date())
        end = datetime.combine(cutoff + timedelta(days=1), datetime.min.time(), NY).timestamp()
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            for table, column in (
                ("minute_observations", "session_date"),
                ("daily_summaries", "session_date"),
                ("session_symbols", "session_date"),
                ("baselines", "session_date"),
                ("market_sessions", "session_date"),
                ("alert_events", "session_date"),
                ("daily_runs", "day"),
                ("api_usage", "day"),
                ("provider_usage", "day"),
                ("portfolio_snapshots", "day"),
            ):
                db.execute(f"DELETE FROM {table} WHERE {column}<=?", (str(cutoff),))
            db.execute("DELETE FROM scan_jobs WHERE status!='running' AND created_at<?", (end,))
            db.execute("DELETE FROM outbox WHERE created_at<?", (now - 86400,))
            db.execute("DELETE FROM provider_calls WHERE requested_at<?", (now - 60,))
            db.execute(
                "DELETE FROM signals WHERE kind IN ('news','news_daily','insiders','briefing') AND event_time<?",
                (end,),
            )
            db.execute(
                "DELETE FROM signals WHERE kind NOT IN ('news','news_daily','insiders','briefing') AND last_seen<? AND event_time<?",
                (end, end),
            )
            db.execute("DELETE FROM signal_notifications WHERE created_at<?", (end,))
            db.execute("DELETE FROM signal_sources WHERE attempted_at<?", (end,))
            db.execute(
                "DELETE FROM state WHERE key LIKE 'news_health_%' AND json_extract(value,'$.checked_at')<?",
                (end,),
            )
            db.execute(
                "DELETE FROM state WHERE key LIKE 'earnings_status_%' AND substr(key,17) NOT IN (SELECT symbol FROM signal_sources WHERE kind='earnings')"
            )
            db.execute(
                "DELETE FROM state WHERE key LIKE 'insiders_warning_%' AND substr(key,18) NOT IN (SELECT symbol FROM signal_sources WHERE kind='insiders')"
            )
            db.execute(
                "DELETE FROM signal_jobs WHERE status!='pending' AND created_at<?", (now - 86400,)
            )
            db.execute("DELETE FROM signal_http_calls WHERE requested_at<?", (now - 3600,))
            db.execute("DELETE FROM signal_http_cache WHERE fetched_at<?", (now - 7 * 86400,))
            db.execute(
                "DELETE FROM state WHERE key LIKE 'briefing_wait_%' AND substr(key,15)<=?",
                (str(cutoff),),
            )
            for kind, cap in (
                ("news", 1000),
                ("news_daily", 4500),
                ("insiders", 5000),
                ("earnings", 2000),
                ("briefing", 100),
            ):
                db.execute(
                    "DELETE FROM signals WHERE kind=? AND rowid NOT IN (SELECT rowid FROM signals WHERE kind=? ORDER BY event_time DESC,last_seen DESC LIMIT ?)",
                    (kind, kind, cap),
                )
            db.execute("DELETE FROM live_quotes WHERE received_at<?", (end,))
            db.execute(
                "DELETE FROM state WHERE key LIKE 'finnhub_profile_%' "
                "AND json_extract(value,'$.checked_at')<? "
                "AND substr(key,17) NOT IN (SELECT ticker FROM watchlist)",
                (end,),
            )
            db.execute(
                "DELETE FROM instruments WHERE symbol NOT IN (SELECT ticker FROM watchlist) "
                "AND id NOT IN (SELECT instrument_id FROM minute_observations) "
                "AND id NOT IN (SELECT instrument_id FROM daily_summaries) "
                "AND id NOT IN (SELECT instrument_id FROM session_symbols)"
            )
            db.execute("INSERT OR REPLACE INTO state VALUES ('retention_cutoff',?)", (str(cutoff),))
            db.execute("INSERT OR REPLACE INTO state VALUES ('last_purge',?)", (str(now),))

    def metrics(self) -> dict:
        sizes = {
            suffix: Path(str(self.store.path) + suffix).stat().st_size
            for suffix in ("", "-wal", "-shm")
            if Path(str(self.store.path) + suffix).exists()
        }
        backups = [
            path
            for path in self.backups.glob("*.sqlite3")
            if AUTO_BACKUP.fullmatch(path.name) and not path.is_symlink()
        ]
        with self.store.connection() as db:
            minutes = db.execute("SELECT COUNT(*) FROM minute_observations").fetchone()[0]
            daily = db.execute("SELECT COUNT(*) FROM daily_summaries").fetchone()[0]
            free_pages = db.execute("PRAGMA freelist_count").fetchone()[0]
        return {
            "database_bytes": sizes.get("", 0),
            "wal_bytes": sizes.get("-wal", 0),
            "shared_memory_bytes": sizes.get("-shm", 0),
            "backup_bytes": sum(p.stat().st_size for p in backups),
            "backup_count": len(backups),
            "minute_rows": minutes,
            "daily_rows": daily,
            "reusable_bytes": free_pages * 4096,
            "free_bytes": shutil.disk_usage(self.store.path.parent).free,
        }

    def notice(self, key: str, text: str):
        if self.store.state(key) == "1":
            return
        if self.owner_id is not None:
            from trabe.outbox import Outbox

            Outbox(self.store).send(self.owner_id, text, kind="notice")
        self.store.set_state(key, "1")

    def guard(self, metrics: dict):
        allowance = (32 if len(self.store.watchlist()) <= 10 else 64) * 1024**2
        total = sum(
            metrics[k]
            for k in ("database_bytes", "wal_bytes", "shared_memory_bytes", "backup_bytes")
        )
        reason = ""
        if metrics["free_bytes"] < 100 * 1024**2:
            reason = (
                "Disk reserve below 100 MiB; collection paused to preserve control/recovery space."
            )
        elif metrics["wal_bytes"] > 64 * 1024**2:
            reason = "WAL exceeds 64 MiB; collection paused until checkpointing recovers."
        elif metrics["database_bytes"] > allowance * 2:
            reason = "Database exceeds the retention allowance; collection paused for maintenance."
        self.store.set_state("storage_paused", reason)
        if reason:
            self.notice("storage_notice", reason + " Use /storage.")
        else:
            self.store.set_state("storage_notice", "0")
        if total > allowance:
            self.notice(
                "storage_size_notice",
                "Storage planning allowance exceeded. Use /storage to inspect history, WAL and backups.",
            )
        else:
            self.store.set_state("storage_size_notice", "0")

    def run_once(self, *, now: float | None = None):
        now = time.time() if now is None else now
        self.expire(now)
        with self.store.connection() as db:
            due = list(
                db.execute(
                    "SELECT session_date FROM market_sessions WHERE finalized=0 AND closes+600<=? ORDER BY session_date",
                    (now,),
                )
            )
        for (day,) in due:
            self.rollup(day, now=now)
        prune_backups(self.backups, now=now)
        # Dedicated connection without an active transaction; readers may return BUSY.
        with self.store.connection() as db:
            busy, _, _ = db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        self.store.set_state(
            "maintenance_checkpoint", "busy; retry scheduled" if busy else "complete"
        )
        self.guard(self.metrics())
        self.store.set_state("maintenance_error", "")
        self.store.set_state("maintenance_last_run", str(now))
        self.last_run = now

    def report(self) -> str:
        values = self.metrics()
        rows = ["trabe storage • local data only"]
        for key in (
            "database_bytes",
            "wal_bytes",
            "shared_memory_bytes",
            "backup_bytes",
            "reusable_bytes",
            "free_bytes",
        ):
            rows.append(
                f"{key.removesuffix('_bytes').replace('_', ' ')}: {values[key] / 1024**2:.2f} MiB"
            )
        rows.extend(
            (
                f"Minute rows: {values['minute_rows']} • daily rows: {values['daily_rows']}",
                f"Automatic backups: {values['backup_count']} (3 retained + up to 1 protected)",
                f"History: 3 calendar months • expire through {self.store.state('retention_cutoff', 'not run')}",
                f"Last rollup: {self.store.state('last_rollup', 'none')}",
                f"Last purge: {self.store.state('last_purge', 'none')}",
                f"Checkpoint: {self.store.state('maintenance_checkpoint', 'not run')}",
                f"Maintenance: {self.store.state('maintenance_error') or 'healthy'}",
                f"Collection: {self.store.state('storage_paused') or 'storage ready'}",
            )
        )
        return "\n".join(rows)
