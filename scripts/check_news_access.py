"""Read-only news entitlement check. Never polls Telegram or opens a runtime DB."""

import argparse
import json
import os
import sys
import tempfile
import time
from dataclasses import replace
from pathlib import Path


def main():
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from trabe.config import Config
    from trabe.news import News, text
    from trabe.news_sources import NewsSources
    from trabe.signals import SignalHTTP
    from trabe.store import Store

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", choices=("massive", "marketaux"), default="massive")
    parser.add_argument("--output", type=Path, default=Path(".local/news-08-probe/access.json"))
    args = parser.parse_args()
    config = Config.load()
    if not getattr(config, args.provider + "_key"):
        raise SystemExit("Save the provider key in .env first.")
    evidence = {"provider": args.provider, "checked_at": time.time(), "symbols": []}
    with tempfile.TemporaryDirectory(prefix="trabe-news-access-") as directory:
        store = Store(Path(directory) / "probe.db")
        http = SignalHTTP(replace(config, database=store.path), store)
        news = News(store, http)
        adapter = NewsSources(http, news._normalize, text)
        for ticker in ("GOOGL", "VST"):
            try:
                items, truncated = adapter.fetch(args.provider, ticker, time.time())
                evidence["symbols"].append(
                    {
                        "ticker": ticker,
                        "access": "ok",
                        "items": len(items),
                        "articles_with_sentiment": sum(bool(i["annotations"]) for i in items),
                        "truncated": truncated,
                        "directions": sorted(
                            {a["direction"] for i in items for a in i["annotations"]}
                        ),
                    }
                )
            except Exception as error:
                evidence["symbols"].append(
                    {"ticker": ticker, "access": "failed", "http_code": getattr(error, "code", 0)}
                )
    args.output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(args.output, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w") as file:
        json.dump(evidence, file, indent=2)
        file.write("\n")
    print(json.dumps(evidence, indent=2))
    return 0 if all(s["access"] == "ok" for s in evidence["symbols"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
