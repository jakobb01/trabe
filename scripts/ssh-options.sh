# Shared by deploy.sh and inspect-server.sh after changing to the project root.
# Read only SSH options; do not source credential files as executable shell code.
ssh_settings=$(python3 - <<'PY'
import os
from pathlib import Path
from stock_agent.config import read_env

try:
    values = read_env(Path(os.environ.get('DEPLOY_ENV_FILE', '.env.deploy'))) | dict(os.environ)
    target = values.get('SSH_TARGET', '').strip()
    if not target or target.startswith('-') or any(c.isspace() for c in target):
        raise ValueError('Set SSH_TARGET in .env.deploy or the environment; see .env.deploy.example.')
    port = values.get('SSH_PORT', '22')
    if not port.isascii() or not port.isdecimal() or not 1 <= int(port) <= 65535:
        raise ValueError('SSH_PORT must be a number from 1 to 65535.')
    options = [target, port, values.get('SSH_IDENTITY_FILE', ''), values.get('SSH_CONFIG_FILE', '')]
    if any('\n' in value or '\r' in value or '\0' in value for value in options):
        raise ValueError('SSH options must be single-line values.')
    print('\n'.join(options))
except (OSError, ValueError) as error:
    raise SystemExit(str(error)) from None
PY
)
mapfile -t ssh_settings <<< "$ssh_settings"
ssh_target=${ssh_settings[0]}
ssh_port=${ssh_settings[1]}
ssh_args=(-p "$ssh_port" -o ConnectTimeout=15)
if [[ -n ${ssh_settings[2]:-} ]]; then
    ssh_args+=(-i "${ssh_settings[2]}" -o IdentitiesOnly=yes)
fi
if [[ -n ${ssh_settings[3]:-} ]]; then
    ssh_args+=(-F "${ssh_settings[3]}")
fi
unset ssh_settings
