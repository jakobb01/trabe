#!/usr/bin/env bash
# Read-only VST diagnosis: no API calls, Telegram requests or runtime writes.
set -euo pipefail
project_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
cd "$project_dir"
source "$project_dir/scripts/ssh-options.sh"
ssh "${ssh_args[@]}" -o BatchMode=yes "$ssh_target" 'podman exec -i stock-agent python -' <<'PY'
import json
import os
import sqlite3
import time
from pathlib import Path

from trabe import __version__

now = time.time()
path = Path(os.environ.get("DATABASE_PATH", "/data/stock_agent.db")).resolve()
db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=3)
report = {"version": __version__, "now": now, "symbol": "VST"}
report["settings"] = dict(db.execute("SELECT key,value FROM settings WHERE key IN ('earnings_enabled','briefing_enabled','finnhub_daily_budget')"))
report["worker_state"] = dict(db.execute("SELECT key,value FROM state WHERE key IN ('insights_error','pause_until','storage_paused','finnhub_retry_after','earnings_status_VST')"))
report["sources"] = [dict(zip(("source", "attempted_at", "succeeded_at", "error", "retry_at"), row)) for row in db.execute("SELECT source,attempted_at,succeeded_at,error,retry_at FROM signal_sources WHERE kind='earnings' AND symbol IN ('VST','*')")]
report["events"] = []
for kind, payload, seen in db.execute("SELECT kind,payload,last_seen FROM signals WHERE symbol='VST' AND kind IN ('earnings','earnings-history') ORDER BY event_time DESC"):
    payload = json.loads(payload)
    fields = ("date", "period", "checked_at", "reported", "epsEstimate", "epsActual", "revenueEstimate", "revenueActual", "comparisons", "result_tracking", "result_seen", "superseded_by", "rows")
    report["events"].append({"kind": kind, "last_seen": seen, "payload": {key: payload[key] for key in fields if key in payload}})
db.close()
print(json.dumps(report, indent=2))
PY
