#!/usr/bin/env bash
set -euo pipefail

fail() { printf '%s\n' "$*" >&2; exit 1; }
[[ $(id -u) != 0 ]] || fail 'Run as your regular SSH user, not root.'
for tool in podman systemctl loginctl tar install flock sha256sum; do
    command -v "$tool" >/dev/null || fail "Missing $tool. See docs/deployment.md."
done
[[ $(podman info --format '{{.Host.CgroupsVersion}}') == v2 ]] || fail 'Quadlet requires cgroup v2.'
mode=${1:-native}
[[ "$mode" == native || "$mode" == auto || "$mode" == bundled ]] || fail 'Invalid Quadlet deployment mode.'
generator_found=false
for generator in /etc/systemd/user-generators/podman-user-generator \
                 /usr/local/lib/systemd/user-generators/podman-user-generator \
                 /usr/lib/systemd/user-generators/podman-user-generator; do
    if [[ -x "$generator" ]]; then generator_found=true; fi
done
selected_mode=native
if [[ "$mode" == bundled || -x "$HOME/.local/libexec/stock-agent/quadlet" ]]; then
    selected_mode=bundled
elif ! "$generator_found"; then
    [[ "$mode" == auto ]] || fail 'Podman Quadlet generator is missing. Use the app-scoped compatibility mode or a Quadlet-capable Podman installation; see docs/deployment.md.'
    selected_mode=bundled
fi
if [[ "$selected_mode" == bundled ]]; then
    [[ $(uname -m) == armv7l ]] || fail 'Bundled compatibility mode currently supports only the ARMv7 Pi.'
    version=$(podman --version)
    [[ "$version" =~ ([0-9]+)\.([0-9]+)\.([0-9]+) ]] || fail 'Could not determine Podman version.'
    major=${BASH_REMATCH[1]}
    minor=${BASH_REMATCH[2]}
    ((major > 4 || (major == 4 && minor >= 3))) || fail 'Compatibility mode requires Podman 4.3 or newer.'
    help=$(podman run --help)
    for flag in --health-on-failure --sdnotify --cgroups --read-only --cap-drop --pids-limit; do
        [[ "$help" == *"$flag"* ]] || fail "Podman lacks required option $flag."
    done
    printf '%s\n' 'Using an isolated trabe Quadlet generator; the host Podman package will not be upgraded.'
    if [[ ! -x /usr/local/lib/systemd/user-generators/stock-agent-quadlet ]]; then
        [[ "$mode" != bundled ]] || fail 'The trabe generator launcher must be installed first; run scripts/deploy.sh from WSL.'
        printf '%s\n' 'STOCK_AGENT_GENERATOR_BOOTSTRAP=required'
    fi
fi
systemctl --user show-environment >/dev/null || fail 'No systemd user manager. Enable lingering and reconnect over SSH.'
[[ $(loginctl show-user "$(id -un)" -p Linger --value) == yes ]] || fail "Enable boot startup first: sudo loginctl enable-linger $(id -un)"
if systemctl is-active --quiet stock-agent.service; then
    fail 'An older system-wide stock-agent.service is active. Stop it before deploying the rootless service to avoid duplicate polling.'
fi
printf 'Host: %s\n' "$(uname -sm)"
podman --version
printf '%s\n' 'Rootless Podman, cgroup v2 and boot startup: ready.'
printf 'STOCK_AGENT_QUADLET_MODE=%s\n' "$selected_mode"
