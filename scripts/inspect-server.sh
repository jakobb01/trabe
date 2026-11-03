#!/usr/bin/env bash
# Read-only host inventory for selecting the correct Debian/Podman setup.
set -euo pipefail
project_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
cd "$project_dir"
source "$project_dir/scripts/ssh-options.sh"
ssh "${ssh_args[@]}" "$ssh_target" '
    uname -sm
    cat /etc/os-release
    podman --version
    podman info --format "architecture={{.Host.Arch}} cgroups={{.Host.CgroupsVersion}}"
    apt-cache policy podman
    dpkg-query -W podman uidmap dbus-user-session 2>/dev/null || true
    loginctl show-user "$(id -un)" -p Linger
    systemctl --user show-environment >/dev/null && echo "systemd user manager: available"
    for path in /usr/lib/systemd/system-generators/podman-system-generator /usr/libexec/podman/quadlet /usr/lib/podman/quadlet; do
        if test -e "$path"; then ls -l "$path"; fi
    done
    apt-cache policy podman-generate-systemd quadlet 2>/dev/null || true
'
