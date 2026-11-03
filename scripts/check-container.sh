#!/usr/bin/env bash
# Offline checks against the real image; requires no user credentials.
set -euo pipefail
project_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
cd "$project_dir"
image=${CHECK_IMAGE:-localhost/stock-agent:check}
runtime=(podman)
if [[ -n ${CHECK_RUNTIME_ROOT:-} ]]; then
    runtime+=(--root "$CHECK_RUNTIME_ROOT/storage" --runroot "$CHECK_RUNTIME_ROOT/run"
              --tmpdir "$CHECK_RUNTIME_ROOT/tmp" --storage-driver vfs)
fi
"${runtime[@]}" build --format=docker --pull=missing -t "$image" -f Containerfile .
mounts=(-v "$project_dir/tests:/app/tests:ro" -v "$project_dir/deploy:/app/deploy:ro"
        -v "$project_dir/scripts:/app/scripts:ro" -v "$project_dir/Containerfile:/app/Containerfile:ro"
        -v "$project_dir/.containerignore:/app/.containerignore:ro"
        -v "$project_dir/pyproject.toml:/app/pyproject.toml:ro")
restrictions=(--rm --network=none --read-only --cap-drop=all --security-opt=no-new-privileges
              --pids-limit=128 --memory=256m)
"${runtime[@]}" run "${restrictions[@]}" "${mounts[@]}" "$image" python -m unittest discover -s tests -q
"${runtime[@]}" run "${restrictions[@]}" "${mounts[@]}" "$image" python -m unittest discover -s tests -p 'integration_*.py' -v
"${runtime[@]}" run "${restrictions[@]}" "$image" python -c '
import os,ssl,sqlite3
from pathlib import Path
from zoneinfo import ZoneInfo
assert os.geteuid()==1000
assert "CapEff:\t0000000000000000" in Path("/proc/self/status").read_text()
try:
    Path("/app/should-not-write").write_text("fail")
except OSError:
    pass
else:
    raise SystemExit("Root filesystem is writable")
print("Runtime verified:", sqlite3.sqlite_version, ssl.OPENSSL_VERSION, ZoneInfo("Europe/Ljubljana"))
'
