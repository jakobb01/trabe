"""Select a capability explicitly; an outage never silently changes price basis."""

import time

from trabe.api import MarketData
from trabe.providers.finnhub import Finnhub


class MarketRouter:
    def __init__(self, config, store):
        self.store = store
        self.alpha = MarketData(config.api_key, store)
        self.finnhub = Finnhub(config.finnhub_key, store)
        self.monitor = None

    @property
    def provider(self):
        return self.store.settings()["market_provider"]

    @property
    def api_key(self):
        return self.finnhub.api_key if self.provider == "finnhub" else self.alpha.api_key

    def quote(self, ticker: str, *, refresh: bool = False):
        if self.provider == "finnhub":
            if not refresh and self.monitor:
                quote = self.monitor.latest(ticker)
                if quote and -2 <= time.time() - quote["source_time"] <= int(
                    self.store.settings()["max_source_age_seconds"]
                ):
                    return quote, True
            return self.finnhub.quote(ticker, refresh=refresh)
        quote, cached = self.alpha.quote(ticker, refresh=refresh)
        return quote | {"provider": "alphavantage", "feed": "end-of-day"}, cached
