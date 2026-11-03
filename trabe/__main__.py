import argparse
import json
import logging
from pathlib import Path

from trabe.api import ApiError, MarketData, Telegram
from trabe.config import Config
from trabe.maintenance import Maintenance, prune_backups, release_backup
from trabe.providers import Finnhub
from trabe.runtime import health, run
from trabe.setup import setup
from trabe.store import Store


def main() -> int:
    parser = argparse.ArgumentParser(prog="trabe", description="Telegram-controlled stock watcher")
    commands = parser.add_subparsers(dest="command")
    for name in ("run", "setup", "doctor", "maintenance", "backup-prune"):
        subparser = commands.add_parser(name)
        subparser.add_argument("--env-file", type=Path, default=Path(".env"))
        if name == "doctor":
            subparser.add_argument("--provider", choices=("finnhub", "alphavantage"))
            subparser.add_argument(
                "--validate-finnhub",
                action="store_true",
                help="Check free-account quotes and record verified credentials",
            )
            subparser.add_argument(
                "--market", action="store_true", help="Fetch/cache one IBM quote"
            )
            subparser.add_argument(
                "--send", action="store_true", help="Send the result to your paired chat"
            )
        if name == "backup-prune":
            subparser.add_argument(
                "--release", help="Release the completed deployment snapshot before pruning"
            )
    health_parser = commands.add_parser("health")
    health_parser.add_argument(
        "--ready", action="store_true", help="Also require a successful Telegram poll"
    )
    backup_parser = commands.add_parser("backup", help="Write a consistent private SQLite backup")
    backup_parser.add_argument("--env-file", type=Path, default=Path(".env"))
    backup_parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    try:
        if args.command == "health":
            return 0 if health(require_poll=args.ready) else 1
        if args.command == "setup":
            setup(args.env_file)
            return 0
        config = Config.load(getattr(args, "env_file", Path(".env")))
        if args.command == "backup-prune":
            directory = config.database.parent / "backups"
            if args.release:
                release_backup(directory, args.release)
            print(json.dumps(prune_backups(directory)))
            return 0
        if args.command == "maintenance":
            maintenance = Maintenance(Store(config.database), config.owner_id)
            maintenance.run_once()
            print(json.dumps(maintenance.metrics()))
            return 0
        if args.command == "backup":
            Store(config.database).backup(args.output)
            print(f"Consistent database backup saved to {args.output} (mode 0600).")
            return 0
        if args.command == "doctor":
            telegram = Telegram(config.token)
            me = telegram.call("getMe")
            if telegram.call("getWebhookInfo").get("url"):
                raise ValueError(
                    "Active webhook detected; long polling requires a dedicated bot without a webhook."
                )
            store = Store(config.database)
            report = [f"Telegram API: connected as @{me['username']}", "SQLite: writable"]
            if args.market:
                provider = args.provider or store.settings()["market_provider"]
                if (
                    not args.provider
                    and provider == "alphavantage"
                    and not config.api_key
                    and config.finnhub_key
                ):
                    # Bootstrap can start with only a Finnhub key; this probe
                    # does not change the owner's selected runtime provider.
                    provider = "finnhub"
                market = (
                    Finnhub(config.finnhub_key, store)
                    if provider == "finnhub"
                    else MarketData(config.api_key, store)
                )
                quote, cached = market.quote("IBM", refresh=True)
                report.append(
                    f"Market API: {provider} IBM {quote['price']:.4f}, trading day {quote['trading_day']} ({'cached' if cached else 'fetched'})"
                )
            if args.validate_finnhub and config.finnhub_key:
                checked = Finnhub(config.finnhub_key, store).validate()
                report.append(
                    "Finnhub free-account quotes and market status: verified (GOOGL, VST)"
                )
                report.append(
                    f"Finnhub market session: {checked['market_status'].get('session') or 'closed'}"
                )
            if args.send:
                telegram.send(config.owner_id, "✅ Deployment check\n" + "\n".join(report))
                report.append("Test message: delivered")
            print("\n".join(report))
            return 0
        run(config)
        return 0
    except (ValueError, ApiError) as error:
        logging.error("%s", error)
        return 1
    except KeyboardInterrupt:
        return 130
    except FileExistsError:
        logging.error("The backup destination already exists. Choose a new output filename.")
        return 1
    except OSError:
        logging.error(
            "Could not access a configuration or state file. Check its path and permissions."
        )
        return 1
    except Exception:
        logging.error("Unexpected failure. Check service configuration and storage permissions.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
