"""Validated, immediately applied preferences shared by bot and maintenance tools."""

import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULTS = {
    "earnings_enabled": "off",
    "insiders_enabled": "off",
    "news_enabled": "off",
    "briefing_enabled": "off",
    "briefing_time": "08:00",
    "earnings_warning_days": "5",
    "insider_min_value_usd": "10000",
    "default_currency": "EUR",
    "portfolio_summary_time": "off",
    "timezone": "Europe/Ljubljana",
    "quote_cache_minutes": "60",
    "api_daily_budget": "20",
    "dip_threshold_percent": "-4",
    "daily_scan_time": "off",
    "market_provider": "alphavantage",
    "monitoring": "off",
    "max_source_age_seconds": "120",
    "baseline_max_gap_percent": "25",
    "finnhub_daily_budget": "2000",
}

DESCRIPTIONS = {
    "earnings_enabled": "Automatic earnings checks and warnings: on or off",
    "insiders_enabled": "Automatic qualifying insider purchase alerts: on or off",
    "news_enabled": "Background company-news collection: on or off",
    "briefing_enabled": "Combined trading-day morning briefing: on or off",
    "briefing_time": "Briefing time in your timezone, HH:MM",
    "earnings_warning_days": "Warn once within 1–14 calendar days before earnings",
    "insider_min_value_usd": "Minimum qualifying insider purchase value in USD, 1000–1000000000",
    "default_currency": "Default currency for trade input and portfolio reporting: EUR or USD",
    "portfolio_summary_time": "Daily portfolio progress at HH:MM, or off",
    "timezone": "Timezone for messages and the daily scan",
    "quote_cache_minutes": "Reuse quotes for 1–1440 minutes",
    "api_daily_budget": "Maximum provider requests per UTC day, 1–25",
    "dip_threshold_percent": "Flag daily falls at or below this percentage, -50 to -0.1",
    "daily_scan_time": "Daily watchlist summary at HH:MM, or off",
    "market_provider": "Selected source: alphavantage (end-of-day) or finnhub (intraday)",
    "monitoring": "Intraday automation: on or off; starts off until enabled",
    "max_source_age_seconds": "Maximum age of a source observation for alerts, 30–600 seconds",
    "baseline_max_gap_percent": "Opening gap requiring baseline review, 10–50 percent",
    "finnhub_daily_budget": "Independent Finnhub REST attempt budget per UTC day, 100–10000",
}


def validate_setting(key: str, value: str) -> str:
    if key not in DEFAULTS:
        raise ValueError("Unknown preference. Use /settings to see available names.")
    if key == "default_currency":
        value = value.upper()
        if value not in ("EUR", "USD"):
            raise ValueError("Currency must be EUR or USD.")
    elif key == "timezone":
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError(
                "Use an IANA timezone, e.g. Europe/Ljubljana or America/New_York."
            ) from None
    elif key == "market_provider":
        if value not in ("alphavantage", "finnhub"):
            raise ValueError("Provider must be alphavantage or finnhub.")
    elif key in (
        "monitoring",
        "earnings_enabled",
        "insiders_enabled",
        "news_enabled",
        "briefing_enabled",
    ):
        if value not in ("on", "off"):
            raise ValueError("Use on or off.")
    elif key == "earnings_warning_days":
        if not value.isascii() or not value.isdecimal() or not 1 <= int(value) <= 14:
            raise ValueError("Earnings warning must be 1–14 calendar days.")
        value = str(int(value))
    elif key == "insider_min_value_usd":
        from decimal import Decimal

        if not re.fullmatch(r"\d{1,10}(?:\.\d{1,2})?", value) or not Decimal(1000) <= Decimal(
            value
        ) <= Decimal(1000000000):
            raise ValueError("Insider minimum must be 1000–1000000000 USD.")
        value = str(Decimal(value))
    elif key in ("max_source_age_seconds", "baseline_max_gap_percent", "finnhub_daily_budget"):
        low, high = {
            "max_source_age_seconds": (30, 600),
            "baseline_max_gap_percent": (10, 50),
            "finnhub_daily_budget": (100, 10000),
        }[key]
        if not value.isascii() or not value.isdecimal() or not low <= int(value) <= high:
            raise ValueError(f"{key} must be an integer from {low} to {high}.")
        value = str(int(value))
    elif key in ("quote_cache_minutes", "api_daily_budget"):
        limit = 1440 if key == "quote_cache_minutes" else 25
        if not value.isascii() or not value.isdecimal() or not 1 <= int(value) <= limit:
            raise ValueError(f"{key} must be an integer from 1 to {limit}.")
        value = str(int(value))
    elif key == "dip_threshold_percent":
        if not re.fullmatch(r"-\d{1,2}(?:\.\d{1,2})?", value) or not -50 <= float(value) <= -0.1:
            raise ValueError("dip_threshold_percent must be between -50 and -0.1, e.g. -4.")
        value = f"{float(value):g}"
    elif key in ("daily_scan_time", "portfolio_summary_time", "briefing_time"):
        value = value.lower()
        if value != "off" and not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
            raise ValueError("Use a 24-hour time such as 23:00, or off.")
        if key == "briefing_time" and value == "off":
            raise ValueError("Set a time such as 08:00; use /briefing off to disable it.")
    return value
