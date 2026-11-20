# trabe

A private Telegram stock assistant for a Raspberry Pi. It tracks your recorded
trades and watchlist, monitors prices, earnings and insider purchases, and provides
market briefings and saved research suggestions. It never places brokerage orders.

Python 3.11+, SQLite and rootless Podman Quadlet. All application traffic is
outbound; no website, webhook or exposed application port is needed.

## Features

- EUR/USD trade ledger, fees, FX conversion and portfolio progress.
- Intraday price monitoring and deduplicated dip alerts.
- Earnings estimates/results, qualified insider purchases and company news.
- Compact briefings with premarket prices, SPY/QQQ, VIX and details on demand.
- Saved pre-open market outlooks with separate later assessments.
- Five-session company research with frozen evidence and explicit data gaps.
- Inline briefing, paged company news, Advisor, portfolio, reports and settings, plus a six-command menu.
- Company-event digests with named sources, issuer feeds and opt-in material news alerts.
- Silent company outcome scoring and a separate USD 2,000 paper experiment.

Outlooks, sentiment and company research use unvalidated rules. Missing or stale
inputs remain explicit. News presentation uses local rules with no hosted model or
new recurring API cost. News alerts default off.
Current version: **0.9.4**, database schema **13**.

## Get started

1. Create a dedicated bot with [BotFather](https://t.me/BotFather) and obtain a
   [Finnhub API key](https://finnhub.io/register).
2. Install the app and pair your private Telegram account:

   ```sh
   python3 -m venv .venv
   . .venv/bin/activate
   python -m pip install -e .
   python -m trabe setup
   ```

3. Add `FINNHUB_API_KEY` to the generated private `.env`. Copy
   [.env.deploy.example](.env.deploy.example) to `.env.deploy` and configure SSH.
4. Follow the host prerequisites in the [project guide](docs/guide.md#deployment),
   then deploy with `bash scripts/deploy.sh`.
5. In Telegram, open `/home` or the command menu. Add your symbols and enable
   the automations you want; new installations start with automation off.

Credentials and runtime data stay outside Git. Use only one running instance per
bot token; develop with fixtures while the Pi is polling.

## Everyday commands

| Command | Purpose |
| --- | --- |
| `/home`, `/help advanced` | Inline navigation and all advanced commands |
| `/news cached VST`, `/news sources` | Sourced cached developments and coverage gaps |
| `/set news_alerts_enabled on` | Opt into fresh material news (also requires `/news on`) |
| `/add VST`, `/watchlist` | Manage watched symbols |
| `/buy VST 2 100 USD`, `/portfolio` | Record an executed trade and inspect holdings |
| `/monitor on`, `/alerts` | Enable monitoring and inspect alert rules |
| `/news VST`, `/earnings VST`, `/insiders VST` | Refresh company reports |
| `/briefing now`, `/briefing cached`, `/briefing details` | Refresh or inspect the overview/full report |
| `/outlook cached`, `/outlook results` | Read saved market expectations and assessments |
| `/advisor VST`, `/advisor evidence VST` | Save company research and inspect its evidence |
| `/advisor track on`, `/advisor results`, `/advisor result ID` | Collect and inspect five-session company outcomes |
| `/paper start`, `/paper decisions`, `/paper results` | Start and inspect the separate virtual stock experiment |
| `/status`, `/settings`, `/pause 24h`, `/resume` | Inspect and control the service |

The [project guide](docs/guide.md) covers commands, source limits, forecast
contracts, retention, deployment and the roadmap. See [CONTRIBUTING.md](CONTRIBUTING.md)
for development and focused testing.
