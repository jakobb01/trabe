# VST earnings: provider gap confirmed

Follow-up: the 0.8.2 Alpha Vantage fallback returned a disputed VST EPS of 1.75.
It was withdrawn and disabled after the owner's correction. Current Yahoo news
explicitly reports basic EPS of 3.25 USD for Q3 2026. See [0.8.3](release-0.8.3.md)
for the free news-evidence fallback and the limits found in all checked feeds.
Pi access is working; the prior SSH limitation below has been resolved.

Investigated 2026-11-07 at 22:31 UTC with the saved Finnhub key. Two successful
live JSON responses were replayed through the current application in a fresh
temporary database, without Telegram requests or changes to runtime state.

## Evidence

Vistra's [official Q3 results release](https://investor.vistracorp.com/2026-11-06-Vistra-Reports-Third-Quarter-2026-Results)
confirms it reported on **2026-11-06**, for the quarter ending 2026-09-30.

The app's exact Finnhub requests returned:

| Request | Observed data |
| --- | --- |
| `calendar/earnings?symbol=VST&from=2026-10-31&to=2027-02-05` | FY2026 Q3, release date 2026-11-06; `epsActual=null`, `revenueActual=null`; EPS estimate 2.991, revenue estimate 6084287722. |
| `stock/earnings?symbol=VST&limit=4` | Latest fiscal period 2026-06-30 (FY2026 Q2); Q3 2026 absent. Older rows are Q1 2026, Q4 2025 and Q3 2025. |

These public response bodies are retained in the
[regression fixture](../tests/fixtures/earnings_vst_provider_gap.json), with no
credentials. Private probe output and the replayed report are in ignored
`.local/earnings-current-probe/access.json`.

## Conclusion and limits

The current VST missing results are a **provider data gap**, not a parser dropping
available Q3 figures. `SignalHTTP.finnhub` makes a network request without a
response cache; the probe used fresh storage and forced refresh. Replaying both
responses produces the unconfirmed-result card. A regression test also injects
a synthetic later Q3 history response and checks that the existing fiscal-period
matching attaches it correctly. Last year's Q3 is not a substitute.

Our design contributes limitations: cached views were the default before the
pending command changes, and both actual-result paths depend on Finnhub. The
Alpha Vantage adapter currently supplies only an upcoming calendar, and is only
used when the primary calendar returns no rows or fails. A valid schedule with
null actuals therefore has no independent results fallback. The two-day intensive
polling limit was another weakness, but it does not explain the missing figures
in these fresh live responses.

The pending changes extend follow-up and make missing results clearer; they do
not fill this provider gap. A substantive next improvement is an independent
results source or issuer-release confirmation. Keep release confirmation separate
from comparable EPS/revenue actuals and estimates; a published release alone
does not justify an EPS beat/miss or mixing GAAP and adjusted figures.

SSH read-only inspection of the Pi failed with **no route to host**, even outside
the sandbox. Its current settings and last successful refresh were not verified;
an additional runtime issue cannot be ruled out. This is not evidence that the
Pi is down. From normal WSL, the read-only helper can save that final evidence:

```sh
bash scripts/diagnose-earnings.sh > .local/earnings-runtime-diagnostic.json
```

This helper reads only version, VST earnings records, source timestamps and
relevant worker settings. It makes no market API or Telegram requests, does not
restart anything and omits credentials and actual positions.
