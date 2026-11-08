#!/usr/bin/env bash
# Read-only report: no service restart, credentials, holdings or Telegram requests.
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
report = {"version": __version__, "now": now}
report["news_settings"] = dict(db.execute("SELECT key,value FROM settings WHERE key IN ('news_enabled','news_sources','massive_daily_budget','snapshot_enabled')"))
report["keys_configured"] = {provider: bool(os.environ.get(provider.upper() + "_API_KEY")) for provider in ("finnhub", "massive", "marketaux")}
report["jobs"] = []
for identity, kind, state, created, encoded in db.execute("SELECT id,kind,status,created_at,symbols FROM signal_jobs ORDER BY id DESC LIMIT 8"):
    payload = json.loads(encoded)
    report["jobs"].append({"id": identity, "kind": kind, "state": state, "age_seconds": round(now-created), "remaining": len(payload.get("remaining", [])), "total": len(payload.get("all", [])), "origin": payload.get("origin", "manual")})
report["sources"] = [dict(zip(("provider", "symbol", "attempted_at", "succeeded_at", "error", "retry_at"), row)) for row in db.execute("SELECT source,symbol,attempted_at,succeeded_at,error,retry_at FROM signal_sources WHERE kind='news' ORDER BY attempted_at DESC LIMIT 12")]
report["worker_state"] = dict(db.execute("SELECT key,value FROM state WHERE key IN ('insights_error','pause_until','storage_paused','massive_retry_after','finnhub_retry_after')"))
report["rows"] = {name: db.execute("SELECT COUNT(*) FROM " + name).fetchone()[0] for name in ("news_articles", "news_revisions", "news_provider_observations", "news_analyses", "outbox")}
db.close()
report["threads"] = []
for task in Path("/proc/1/task").iterdir():
    try:
        fields = (task / "stat").read_text().split(") ", 1)[1].split()
        report["threads"].append({"id": task.name, "state": fields[0], "cpu_ticks": int(fields[11])+int(fields[12]), "waiting_on": (task / "wchan").read_text().strip()})
    except (OSError, ValueError):
        pass
print(json.dumps(report, indent=2))
PY
