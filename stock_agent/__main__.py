import argparse
import logging
from pathlib import Path

from stock_agent.api import ApiError, MarketData, Telegram
from stock_agent.config import Config
from stock_agent.runtime import health, run
from stock_agent.setup import setup
from stock_agent.store import Store


def main() -> int:
    parser = argparse.ArgumentParser(description="Telegram-controlled StockPulse Sentinel")
    commands = parser.add_subparsers(dest="command")
    for name in ("run", "setup", "doctor"):
        subparser = commands.add_parser(name)
        subparser.add_argument("--env-file", type=Path, default=Path(".env"))
        if name == "doctor":
            subparser.add_argument(
                "--market", action="store_true", help="Fetch/cache one IBM quote"
            )
            subparser.add_argument(
                "--send", action="store_true", help="Send the result to your paired chat"
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
                quote, cached = MarketData(config.api_key, store).quote("IBM", refresh=True)
                report.append(
                    f"Market API: IBM {quote['price']:.4f}, trading day {quote['trading_day']} ({'cached' if cached else 'fetched'})"
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
