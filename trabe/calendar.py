"""Versioned US equity sessions with explicit coverage and exchange-local dates."""

import calendar
import json
import math
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")
DATA_PATH = Path(__file__).parent / "data/us_sessions.json"


def retention_cutoff(day: date, months: int = 3) -> date:
    value = day.year * 12 + day.month - 1 - months
    year, month = divmod(value, 12)
    month += 1
    return date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


@dataclass(frozen=True)
class Session:
    day: str
    opens: float
    closes: float

    @property
    def minutes(self) -> int:
        return int((self.closes - self.opens) / 60)

    def contains(self, timestamp: float) -> bool:
        return self.opens <= timestamp < self.closes


class USCalendar:
    def __init__(self, path: Path = DATA_PATH):
        self.data = json.loads(path.read_text())

    def session(self, day: date) -> Session | None:
        if str(day.year) not in self.data["years"]:
            raise ValueError(
                "US session calendar coverage expired; update trabe before monitoring."
            )
        year = self.data["years"][str(day.year)]
        if day.weekday() >= 5 or day.isoformat() in year["closed"]:
            return None
        closing = time.fromisoformat(year["early_close"].get(day.isoformat(), "16:00"))
        return Session(
            day.isoformat(),
            datetime.combine(day, time(9, 30), NY).timestamp(),
            datetime.combine(day, closing, NY).timestamp(),
        )

    def at(self, timestamp: float) -> Session | None:
        return self.session(datetime.fromtimestamp(timestamp, NY).date())

    def previous(self, day: date) -> Session:
        for offset in range(1, 15):
            session = self.session(day - timedelta(days=offset))
            if session:
                return session
        raise ValueError("No verified previous exchange session in calendar.")

    def next_session(self, timestamp: float) -> Session:
        day = datetime.fromtimestamp(timestamp, NY).date()
        for offset in range(15):
            session = self.session(day + timedelta(days=offset))
            if session and session.opens > timestamp:
                return session
        raise ValueError("No verified upcoming exchange session in calendar.")

    def describe(self, timestamp: float, timezone: str | None = None) -> str:
        session = self.at(timestamp)
        if timezone is not None:
            zone = ZoneInfo(timezone)

            def local(stamp):
                return datetime.fromtimestamp(stamp, zone).strftime("%Y-%m-%d %H:%M %Z")

            if session and session.contains(timestamp):
                return f"regular session • opened {local(session.opens)} • closes {local(session.closes)}"
            upcoming = self.next_session(timestamp)
            minutes = math.ceil((upcoming.opens - timestamp) / 60)
            days, minutes = divmod(minutes, 1440)
            hours, minutes = divmod(minutes, 60)
            countdown = (f"{days}d " if days else "") + (f"{hours}h " if hours else "")
            countdown += f"{minutes}m"
            prefix = (
                "closed (weekend/holiday) • next "
                if session is None
                else "closed • next "
                if timestamp >= session.closes
                else ""
            )
            return f"{prefix}opens {local(upcoming.opens)} (in {countdown}) • closes {local(upcoming.closes)}"
        if session is None:
            return "closed (weekend/holiday)"
        if session.contains(timestamp):
            return "regular session"
        edge = session.opens if timestamp < session.opens else session.closes
        label = "opens" if timestamp < session.opens else "closed"
        return f"{label} {datetime.fromtimestamp(edge, UTC).isoformat()}"
