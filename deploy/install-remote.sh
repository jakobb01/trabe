#!/usr/bin/env bash
# Invoked by scripts/deploy.sh over the existing SSH connection.
set -euo pipefail
source_dir=$1
environment_file=$2
mode=native
if [[ -f "$source_dir/deploy/tools/quadlet" ]]; then mode=bundled; fi
bash "$source_dir/deploy/preflight.sh" "$mode"
if [[ "$mode" == bundled ]]; then
    (cd "$source_dir/deploy/tools" && sha256sum -c SHA256SUMS)
fi

# Optional third argument isolates filesystem effects in deployment tests.
account_home=${3:-$HOME}
state_dir="$account_home/.local/share/stock-agent"
config_dir="$account_home/.config/stock-agent"
unit_dir="$account_home/.config/containers/systemd"
tool_dir="$account_home/.local/libexec/stock-agent"
if [[ "$mode" == bundled ]]; then unit_dir="$config_dir/quadlet"; fi
install -d -m 700 "$state_dir" "$config_dir"
mkdir -p "$unit_dir"
exec 9>"$state_dir/deploy.lock"
flock -n 9 || { printf '%s\n' 'Another stock-agent deployment is in progress.' >&2; exit 1; }

release=$(cd "$source_dir" && { sha256sum Containerfile; find trabe deploy/runtime -type f -print0 | sort -z | xargs -0 sha256sum; } | sha256sum | cut -c1-16)
image="localhost/stock-agent:build-$release"
# The Pi builds its own architecture; no registry account or exposed build daemon.
podman build --format=docker --pull=missing -t "$image" -f "$source_dir/Containerfile" "$source_dir"

# Use SQLite's online backup API before replacing a running release. This also
# works with the original image, without depending on a new application command.
database_backup=
if podman container exists stock-agent && [[ $(podman inspect stock-agent --format '{{.State.Running}}') == true ]]; then
    database_backup=$(podman exec -i stock-agent python - <<'PY'
import fcntl,json,os,sqlite3,time,uuid
from pathlib import Path
database=Path(os.environ['DATABASE_PATH'])
directory=database.parent/'backups'
directory.mkdir(mode=0o700,exist_ok=True)
name='pre-deploy-'+time.strftime('%Y%m%dT%H%M%SZ',time.gmtime())+'-'+uuid.uuid4().hex[:8]+'.sqlite3'
destination=directory/name
lock=os.open(directory/'.backup.lock',os.O_CREAT|os.O_RDWR,0o600)
fcntl.flock(lock,fcntl.LOCK_EX)
marker=directory/'.protected.json'
temporary=directory/('.protected-'+uuid.uuid4().hex+'.tmp')
temporary.write_text(json.dumps({'name':name}))
temporary.chmod(0o600)
os.replace(temporary,marker)
source=target=None
try:
    descriptor=os.open(destination,os.O_CREAT|os.O_EXCL|os.O_RDWR,0o600)
    os.close(descriptor)
    source=sqlite3.connect(database)
    target=sqlite3.connect(destination)
    source.backup(target)
except BaseException:
    destination.unlink(missing_ok=True)
    marker.unlink(missing_ok=True)
    raise
finally:
    if target is not None: target.close()
    if source is not None: source.close()
    os.close(lock)
print(name)
PY
)
    [[ "$database_backup" =~ ^pre-deploy-[A-Za-z0-9-]+\.sqlite3$ ]]
    printf 'Database snapshot retained: %s\n' "$database_backup"
fi

backup_dir=$(mktemp -d "$state_dir/backup.XXXXXXXX")
chmod 700 "$backup_dir"
previous=false
if podman image exists localhost/stock-agent:current \
    && [[ -f "$unit_dir/stock-agent.container" && -f "$config_dir/agent.env" ]]; then
    podman tag localhost/stock-agent:current localhost/stock-agent:previous
    previous=true
fi
for unit in stock-agent.container stock-agent-data.volume; do
    if [[ -f "$unit_dir/$unit" ]]; then cp "$unit_dir/$unit" "$backup_dir/$unit"; fi
done
if [[ -f "$config_dir/agent.env" ]]; then
    install -m 600 "$config_dir/agent.env" "$backup_dir/agent.env"
fi
if [[ "$mode" == bundled ]]; then
    if [[ -f "$tool_dir/quadlet" ]]; then cp "$tool_dir/quadlet" "$backup_dir/quadlet"; fi
fi

rollback() {
    trap - ERR
    printf '%s\n' 'Service state at failure (before rollback):' >&2
    systemctl --user status stock-agent.service --no-pager -l >&2 || true
    journalctl --user -u stock-agent.service -n 60 --no-pager >&2 || true
    if podman container exists stock-agent; then
        podman logs --tail 60 stock-agent >&2 || true
    fi
    printf '%s\n' 'Deployment verification failed; restoring the previous service configuration.' >&2
    systemctl --user stop stock-agent.service || true
    for unit in stock-agent.container stock-agent-data.volume; do
        if [[ -f "$backup_dir/$unit" ]]; then
            cp "$backup_dir/$unit" "$unit_dir/$unit"
        else
            rm -f "$unit_dir/$unit"
        fi
    done
    if [[ -f "$backup_dir/agent.env" ]]; then
        install -m 600 "$backup_dir/agent.env" "$config_dir/agent.env"
    else
        rm -f "$config_dir/agent.env"
    fi
    if [[ "$mode" == bundled ]]; then
        if [[ -f "$backup_dir/quadlet" ]]; then
            install -m 700 "$backup_dir/quadlet" "$tool_dir/quadlet"
        else
            rm -f "$tool_dir/quadlet"
        fi
    fi
    systemctl --user daemon-reload || true
    if "$previous"; then
        podman tag localhost/stock-agent:previous localhost/stock-agent:current
        if [[ -n "$database_backup" ]]; then
            # All application processes are stopped; restore schema and user data
            # together before starting the old release. The snapshot is retained.
            if ! podman run --rm --network=none --read-only --cap-drop=all \
                --security-opt=no-new-privileges -v stock-agent-data:/data:U "$image" \
                python -c 'import sqlite3,sys; from pathlib import Path; backup=Path("/data/backups")/sys.argv[1]; source=sqlite3.connect(backup.as_uri()+"?mode=ro",uri=True); target=sqlite3.connect("/data/stock_agent.db"); source.backup(target); target.close(); source.close()' "$database_backup"; then
                printf 'Database restore failed. Snapshot retained in /data/backups/%s\n' "$database_backup" >&2
            fi
        fi
        systemctl --user start stock-agent.service || true
    fi
    if [[ -n "$database_backup" && -f "$config_dir/agent.env" ]]; then
        # Prune only files: this must not migrate a restored older database.
        podman run --rm --network=none --read-only --cap-drop=all \
            --security-opt=no-new-privileges --env-file "$config_dir/agent.env" \
            -v stock-agent-data:/data:U "$image" \
            python -m trabe backup-prune --release "$database_backup" || true
    fi
    rm -rf "$backup_dir"
    printf '%s\n' 'Inspect: journalctl --user -u stock-agent.service -n 80 --no-pager' >&2
    exit 1
}
trap rollback ERR

podman tag "$image" localhost/stock-agent:current
install -m 600 "$environment_file" "$config_dir/agent.env"
install -m 644 "$source_dir/deploy/quadlet/stock-agent.container" "$unit_dir/stock-agent.container"
rm -f "$unit_dir/stock-agent-data.volume"
if [[ "$mode" == bundled ]]; then
    install -d -m 700 "$tool_dir"
    install -m 700 "$source_dir/deploy/tools/quadlet" "$tool_dir/quadlet"
fi
systemctl --user daemon-reload
systemctl --user restart stock-agent.service

wait_ready() {
    for ((attempt=0; attempt<45; attempt++)); do
        if podman exec stock-agent python -m trabe health --ready >/dev/null 2>&1; then
            return 0
        fi
        sleep 2
    done
    return 1
}
wait_ready
# Validate the market API first. Announce deployment only after restart succeeds.
podman exec stock-agent python -m trabe doctor --market --validate-finnhub
# A real restart must preserve the watchlist and all user preferences.
state_digest='import hashlib,json,os; from pathlib import Path; from trabe.store import Store; store=Store(Path(os.environ["DATABASE_PATH"])); data={"settings":store.settings(),"watchlist":store.watchlist()}; print(hashlib.sha256(json.dumps(data,sort_keys=True).encode()).hexdigest())'
before_restart=$(podman exec stock-agent python -c "$state_digest")
systemctl --user restart stock-agent.service
wait_ready
after_restart=$(podman exec stock-agent python -c "$state_digest")
[[ "$before_restart" == "$after_restart" ]]
systemctl --user is-active stock-agent.service
[[ -z $(podman port stock-agent) ]]
if [[ -n "$database_backup" ]]; then
    podman exec stock-agent python -m trabe backup-prune --release "$database_backup"
else
    podman exec stock-agent python -m trabe backup-prune
fi
podman exec stock-agent python -m trabe doctor --send

trap - ERR
rm -rf "$backup_dir"
printf '%s\n' 'Deployment verified: running, Telegram delivered, market API responded, restart preserved state, no published ports.'
printf '%s\n' 'In Telegram, send /status, /add IBM 1, /quote IBM, /settings.'
