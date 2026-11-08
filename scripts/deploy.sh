#!/usr/bin/env bash
set -euo pipefail
project_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
cd "$project_dir"

source "$project_dir/scripts/ssh-options.sh"
bundle_dir=$(mktemp -d "${TMPDIR:-/tmp}/stock-agent-deploy.XXXXXXXX")
chmod 700 "$bundle_dir"
ssh_args+=(-o ControlMaster=auto -o ControlPersist=60 -o "ControlPath=$bundle_dir/ssh-control")
cleanup() {
    ssh "${ssh_args[@]}" -O exit "$ssh_target" >/dev/null 2>&1 || true
    rm -rf "$bundle_dir"
}
trap cleanup EXIT
python3 - "$bundle_dir/agent.env" <<'PY'
import sys
from pathlib import Path
from trabe.config import Config
from trabe.setup import write_credentials
try:
    config = Config.load()
    if not config.api_key and not config.finnhub_key:
        raise ValueError('Configure a market provider key before deployment.')
    write_credentials(Path(sys.argv[1]), config.token, config.owner_id, config.api_key,
                      config.finnhub_key, config.massive_key, config.marketaux_key)
except ValueError as error:
    raise SystemExit(str(error)) from None
PY

# Establish normal interactive SSH trust/authentication once, then reuse it.
# Host-key changes still fail normally; no StrictHostKeyChecking bypass is used.
ssh "${ssh_args[@]}" -fN "$ssh_target"
# Check prerequisites before uploading credentials or building anything.
ssh "${ssh_args[@]}" "$ssh_target" 'bash -s -- auto' < deploy/preflight.sh > "$bundle_dir/preflight.log"
cat "$bundle_dir/preflight.log"
selected_mode=native
while IFS= read -r line; do
    if [[ "$line" == STOCK_AGENT_QUADLET_MODE=bundled ]]; then selected_mode=bundled; fi
done < "$bundle_dir/preflight.log"
python3 scripts/prepare_runtime.py
python3 - "$bundle_dir/source" <<'PY'
import shutil,sys
from pathlib import Path
destination=Path(sys.argv[1])
destination.mkdir()
for name in ('Containerfile','.containerignore','pyproject.toml'):
    shutil.copy2(name,destination/name)
for name in ('trabe','deploy'):
    shutil.copytree(name,destination/name,ignore=shutil.ignore_patterns('__pycache__'))
PY
if [[ "$selected_mode" == bundled ]]; then
    python3 scripts/fetch_quadlet.py
    # systemd reads user generators from system directories, not ~/.config.
    # Install one small, gated launcher. It never replaces the Podman package.
    ssh "${ssh_args[@]}" "$ssh_target" '
        umask 077
        mkdir -p "$HOME/.local/share/stock-agent"
        cat > "$HOME/.local/share/stock-agent/quadlet-generator-launcher"
    ' < deploy/stock-agent-quadlet-generator
    ssh "${ssh_args[@]}" -tt "$ssh_target" '
        source_file="$HOME/.local/share/stock-agent/quadlet-generator-launcher"
        target_file=/usr/local/lib/systemd/user-generators/stock-agent-quadlet
        if test -e "$target_file"; then
            if ! cmp -s "$source_file" "$target_file"; then
                # 0.2.2 only renames a comment in the 0.2.1 launcher. Accept this
                # exact pair without replacing the already installed root file.
                if ! printf "%s  %s\n" \
                    c8440032a359091c1e834f29b0d75576187c7420aa99e2ebec27827fd1d3700c "$source_file" \
                    b828bc2d909124b0595fdcf8f6bcfe2f85238d124bdda522a7538929a9ef5fb0 "$target_file" \
                    | sha256sum -c --status; then
                    echo "A different trabe generator launcher already exists. Review it before replacing it." >&2
                    exit 1
                fi
            fi
        else
            echo "Installing the app-scoped Quadlet launcher; sudo may request your password."
            sudo install -D -o root -g root -m 755 "$source_file" "$target_file"
        fi
    '
    python3 - "$bundle_dir/source/deploy/tools" <<'PY'
import hashlib,json,shutil,sys
from pathlib import Path
destination=Path(sys.argv[1]);destination.mkdir()
source=Path('.local/tooling/quadlet/armhf/quadlet')
expected=json.loads(Path('deploy/tools-lock.json').read_text())['quadlet']['armhf']['binary_sha256']
if hashlib.sha256(source.read_bytes()).hexdigest()!=expected:
    raise SystemExit('Quadlet binary checksum mismatch.')
shutil.copy2(source,destination/'quadlet')
(destination/'SHA256SUMS').write_text(expected+'  quadlet\n')
PY
fi
tar -czf "$bundle_dir/source.tar.gz" -C "$bundle_dir/source" \
    Containerfile .containerignore pyproject.toml trabe deploy
tar -czf "$bundle_dir/bundle.tar.gz" -C "$bundle_dir" agent.env source.tar.gz
ssh "${ssh_args[@]}" "$ssh_target" '
    set -eu
    umask 077
    mkdir -p "$HOME/.local/share/stock-agent"
    task_dir=$(mktemp -d "$HOME/.local/share/stock-agent/incoming.XXXXXXXX")
    trap '\''rm -rf "$task_dir"'\'' EXIT
    tar -xzf - -C "$task_dir"
    mkdir "$task_dir/source"
    tar -xzf "$task_dir/source.tar.gz" -C "$task_dir/source"
    bash "$task_dir/source/deploy/install-remote.sh" "$task_dir/source" "$task_dir/agent.env"
' < "$bundle_dir/bundle.tar.gz"
