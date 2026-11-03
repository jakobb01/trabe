"""Bootstrap credentials. User preferences live in SQLite, not environment files."""

import os
import re
from dataclasses import dataclass, field
from pathlib import Path


def read_env(path: Path) -> dict[str, str]:
    """Read simple KEY=value files without executing shell expressions."""
    values = {}
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            key, separator, value = line.partition("=")
            if not separator or not re.fullmatch(r"[A-Z][A-Z0-9_]*", key.strip()):
                raise ValueError("Invalid environment file; use KEY=value lines.")
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            values[key.strip()] = value
    return values


@dataclass(frozen=True)
class Config:
    token: str = field(repr=False)
    owner_id: int
    api_key: str = field(repr=False)
    database: Path
    finnhub_key: str = field(default="", repr=False)

    def __post_init__(self):
        if not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]{20,}", self.token):
            raise ValueError("Set a valid TELEGRAM_BOT_TOKEN; run python3 -m trabe setup.")
        if type(self.owner_id) is not int or not 0 < self.owner_id < 2**52:
            raise ValueError("Pair your private Telegram chat: python3 -m trabe setup.")
        if self.api_key and not re.fullmatch(r"[A-Za-z0-9_-]+", self.api_key):
            raise ValueError("ALPHA_VANTAGE_API_KEY contains invalid characters.")
        if self.finnhub_key and not re.fullmatch(r"[A-Za-z0-9_-]+", self.finnhub_key):
            raise ValueError("FINNHUB_API_KEY contains invalid characters.")
        if not isinstance(self.database, Path) or str(self.database) == ".":
            raise ValueError("DATABASE_PATH must name a database file.")

    @classmethod
    def load(cls, path: Path = Path(".env")) -> "Config":
        values = read_env(path) | dict(os.environ)
        token = values.get("TELEGRAM_BOT_TOKEN", "").strip()
        owner = values.get("TELEGRAM_CHAT_ID", "").strip()
        api_key = values.get("ALPHA_VANTAGE_API_KEY", "").strip()
        if not owner.isascii() or not owner.isdecimal() or len(owner) > 16:
            raise ValueError("Pair your private Telegram chat: python3 -m trabe setup.")
        database = Path(values.get("DATABASE_PATH", "data/stock_agent.db"))
        return cls(token, int(owner), api_key, database, values.get("FINNHUB_API_KEY", "").strip())
