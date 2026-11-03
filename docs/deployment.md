# Raspberry Pi deployment

## Network and credentials

The Pi uses outbound HTTPS long polling to Telegram and outbound HTTPS/WebSocket
connections for market data. It opens no application listener or published container port. Deployment uses
your existing SSH connection, configured locally in `.env.deploy`.
See Telegram's [getUpdates documentation](https://core.telegram.org/bots/api#getupdates).
The app refuses to overwrite an existing webhook.

Run `python3 -m trabe setup` to create and pair a private `.env`. Daily
preferences and the watchlist are managed in Telegram. To change bot/account or
rotate credentials, update `.env`, re-pair where appropriate, and redeploy.

## Intraday upgrade (0.3)

Add `FINNHUB_API_KEY` privately to `.env` before deploying. The Finnhub webhook
secret is not needed. The installer stages a checksum-verified pure-Python
WebSocket dependency and copies it into the ARM image; no new host package is
required. SQLite schema 3 preserves existing watchlist, settings, usage and replies.
The pre-deploy database snapshot supports rollback to the previous schema.

After deployment, select `/provider finnhub` and enable `/monitor on` in Telegram.
Monitoring starts off. `/status` reports subscriptions, session, last source time
and failures. `/pause 24h` stops automation while manual controls and retention
continue. `/resume` clears the pause. `/storage` and `/history VST 5` need no
market requests. See [the README](../README.md) for alert rules and data limits.

## Upgrading to the trabe name (0.2.2)

The Python package and installed command are now `trabe`: use `python3 -m trabe`
or the installed `trabe` command. Telegram help, status, startup and test messages
use the same name. Run the usual deployment command below to update the Pi.

Existing installations retain `stock-agent.service`, the `stock-agent` container,
image tags, configuration paths, `stock-agent-data` volume and `stock_agent.db`
filename. These identifiers preserve the running installation and its saved data.
There is no database migration for this rename. The root-owned 0.2.1 launcher is
accepted by its exact checksum because the new source changes only a comment;
an already bootstrapped Pi needs no new administrator command.

## Configure and deploy

Requires Python 3.11+, Bash, OpenSSH and tar on the deployment machine. WSL is
supported. Run from the project directory:

```sh
cp .env.deploy.example .env.deploy
```

Edit `.env.deploy` with your SSH destination. For example:

```dotenv
SSH_TARGET=containers@raspberrypi.local
SSH_PORT=22
```

This file accepts plain `KEY=value` lines and quoted values; it is read as text,
never executed as shell code. It is excluded from Git and container builds. SSH
passwords are entered interactively and must not be stored here. To deploy:

```sh
mkdir -p .local
set -o pipefail
bash scripts/deploy.sh 2>&1 | tee .local/deploy.log
```

SSH establishes trust/authentication interactively and reuses one connection for
preflight and transfer. Verify a new host fingerprint normally. A conflicting host
key is not bypassed. Existing keys, an agent or password authentication work.

Optional overrides:

```sh
SSH_IDENTITY_FILE="$HOME/.ssh/id_ed25519" bash scripts/deploy.sh
SSH_TARGET=containers@another-host SSH_PORT=2222 bash scripts/deploy.sh
# Only if the normal SSH configuration is broken:
SSH_CONFIG_FILE=/dev/null SSH_IDENTITY_FILE="$HOME/.ssh/id_ed25519" bash scripts/deploy.sh
```

Environment variables override `.env.deploy`. `DEPLOY_ENV_FILE` can select a
different local settings file. The same options apply to `scripts/inspect-server.sh`.

Windows keys under `/mnt/c/Users/<user>/.ssh` may have permissions OpenSSH rejects.
Use a private WSL copy with mode 0600 if needed, without overwriting an existing key.

## ARMv7 and Podman 4.3 compatibility

Deployment has been verified on **32-bit ARMv7 Raspbian 12**, Podman **4.3.1**,
cgroup v2, a working systemd user manager and enabled lingering. Podman 4.3
predates integrated Quadlet. The compatibility path retains that host package.

The deployment script selects native Quadlet when a suitable user generator is
present. Otherwise, on this ARMv7 host, it prepares a pinned Debian ARMhf Quadlet
generator. `deploy/tools-lock.json` records the official package URL, package SHA256
and extracted binary SHA256. Preparation verifies both checksums and extracts only
the generator; it does **not** install the newer Podman package or its dependencies.

The ARM generator has been executed under ARM emulation against a Bookworm image.
Its maximum referenced GLIBC version is 2.34, compatible with Bookworm's 2.36.
The generated start/stop commands are checked against an actual Podman 4.3 parser.
The volume uses normal lazy named-volume creation: modern Quadlet's separate
`volume create --ignore` command is incompatible with Podman 4.3 and is avoided.

Systemd's user-generator directories are system-level directories; it does not
search `~/.config/systemd/user-generators`. Therefore compatibility deployment
installs one small launcher, [deploy/stock-agent-quadlet-generator](../deploy/stock-agent-quadlet-generator),
as a root-owned mode-0755 file at:

```text
/usr/local/lib/systemd/user-generators/stock-agent-quadlet
```

The normal deployment command requests `sudo` authentication for this one-time
installation if needed. The launcher runs as each user, exits immediately for users
without trabe, and restricts the real generator to this app's private Quadlet
directory. It does not read other apps' definitions or replace any installed runtime.
An existing different launcher at that path causes a stop for review.

For a container account without sudo, the first deployment stages the launcher
before its installation attempt. An administrator can install it from a separate
account with this single command, substituting the container account's home path:

```sh
sudo install -D -o root -g root -m 755 \
  /home/containers/.local/share/stock-agent/quadlet-generator-launcher \
  /usr/local/lib/systemd/user-generators/stock-agent-quadlet
```

Then rerun deployment as the container account. After that bootstrap, application
updates are rootless. A failed initial install removes its private generator and
definitions; the small root-owned launcher can
remain inert. Normal deployment and container management use the unprivileged
container account.

References: [systemd generator search paths](https://www.freedesktop.org/software/systemd/man/latest/systemd.generator.html),
[Podman Quadlet](https://docs.podman.io/en/latest/markdown/podman-systemd.unit.5.html),
[Debian ARMhf package](https://packages.debian.org/trixie/armhf/podman).

## Other host prerequisites

Preflight requires a regular user, working rootless Podman, cgroup v2, systemd user
services and lingering. It checks these before uploading application credentials.
Compatibility mode requires ARMv7 and Podman 4.3+; other hosts need native Quadlet.
An administrator can install missing distribution prerequisites and enable boot
startup:

```sh
sudo apt-get update
sudo apt-get install -y podman uidmap slirp4netns fuse-overlayfs dbus-user-session
sudo loginctl enable-linger containers
```

Replace `containers` with the service account name. Check which prerequisites are
already configured before installing packages. The compatibility path avoids an
OS upgrade or mixing package repositories merely to obtain a newer Podman.

An active old system-wide `stock-agent.service` stops preflight, to avoid competing
pollers. Inspect that exact service before deciding whether to replace it. Existing
unrelated containers/services are not stopped by deployment.

To capture another read-only inventory:

```sh
bash scripts/inspect-server.sh 2>&1 | tee .local/server-info.log
```

## What the installer verifies

1. Build the target architecture from the pinned multi-architecture Python image.
   The build copies code only and executes no target-CPU `apt` or `pip` steps.
2. Back up a running previous SQLite database with SQLite's online backup API.
3. Retain the previous image/configuration, install definitions and start the service.
4. Require liveness **and a successful Telegram poll**.
5. Fetch a fresh IBM quote using the selected provider, and validate GOOGL/VST
   plus market status when a Finnhub key is present.
6. Restart the service and compare watchlist/settings fingerprints before and after.
7. Require an active service and zero published ports, then send the deployment
   confirmation to Telegram.

Failures after replacement restore the previous image, credentials and definitions.
Where a previous database snapshot exists, rollback restores it before starting
the older release. The snapshot is retained. Failed builds or backups leave the
running release untouched. New data entered during a failed rollout can be lost
when its pre-deploy snapshot is restored.

Diagnostics count provider attempts normally and cache their quotes. Finnhub
validation may need profile requests as well as quotes. The previous prototype's
database is not automatically imported.

## Telegram acceptance check

After successful deployment, send:

```text
/menu
/status
/add IBM 1
/quote IBM
/prices
/scan
/lastscan
/set quote_cache_minutes 120
/settings
```

The quote shows its actual trading day. `/prices` spends no quota. The scan produces
one summary and `/status` remains usable. The changed preference and IBM watchlist
entry should survive a service restart. Leave scheduling off during the first test;
enable it later with `/schedule 23:00`, and disable it with `/schedule off`.

## Files and maintenance

| Location | Purpose |
| --- | --- |
| `~/.config/stock-agent/agent.env` | Credentials, mode 0600 |
| `~/.config/containers/systemd/stock-agent.container` | Native Quadlet definition |
| `~/.config/stock-agent/quadlet/stock-agent.container` | Compatibility mode definition |
| `~/.local/libexec/stock-agent/quadlet` | Verified per-app compatibility binary, mode 0700 |
| Podman volume `stock-agent-data` | SQLite state, reply queue and database backups |
| `localhost/stock-agent:current` / `:previous` | Active and previous images |

```sh
systemctl --user status stock-agent.service
journalctl --user -u stock-agent.service -n 80 --no-pager
podman exec stock-agent python -m trabe health --ready
podman inspect stock-agent --format '{{.State.Health.Status}}'
podman port stock-agent
systemctl --user restart stock-agent.service
```

`podman port` should print nothing. Quadlet's `[Install]` section supplies the boot
dependency; do not enable its generated transient unit manually. Lingering starts
the user manager at boot and keeps it alive after SSH logout. Reboot verification
on the actual Pi remains a separate check when its other services can be interrupted.

The process runs as UID 1000 inside the rootless container, with a read-only root
filesystem and all capabilities dropped. Its named volume is writable; `:U` maps
ownership for that container user. Logs go to journald.

Consistent private online backup:

```sh
podman exec stock-agent python -m trabe backup --output /data/manual-backup.sqlite3
podman cp stock-agent:/data/manual-backup.sqlite3 ./manual-backup.sqlite3
chmod 600 ./manual-backup.sqlite3
```

Choose a new destination filename for each backup. Pre-deployment backups live in
`/data/backups/`. Keep credentials backed up separately and never delete the named
volume during an update.

Automatic backups are pruned to three retained snapshots plus one protected while
deployment/rollback is active, and expire after three calendar months. Maintenance
shares the protection lock with deployment. Manual snapshots are not pruned.
Session rollup runs at close plus ten minutes and deletes minute detail atomically;
retention catches up after downtime or restoration even when monitoring is off.

## Failure behavior

Network calls have timeouts; polling backs off and respects HTTP 429 retry delays.
Liveness is separate from upstream connectivity, so a temporary Telegram outage
does not cause repeated restart notices. Deployment readiness additionally requires
a recent successful poll. Startup notifications are sent once per app version.

Replies are persisted before delivery, split by Telegram's UTF-16 length limits,
and paced. Successful chunks are acknowledged individually. Pending replies survive
restart; entries older than 24 hours expire. A lost HTTP response can still cause a
duplicate because Telegram offers no sendMessage idempotency key.

The incoming update offset is stored before dispatch, preventing repeated API work
after a crash. A crash between claiming an update and completing its command can
still lose that command. Resend it or inspect `/watchlist`, `/settings` and `/lastscan`.
Only one process may poll using a given database; a second local instance is refused.
