#!/usr/bin/env bash
# install.sh - set up hytale-idle on a Linux server that runs Hytale in Docker.
#
#   sudo ./install.sh                     # asks a few questions
#   sudo ./install.sh --container hytale --idle-minutes 15 --yes
#   sudo ./install.sh --uninstall
#   ./install.sh --dry-run                # shows what it would do, changes nothing (no root needed)
#
# What it changes: /usr/local/bin/hytale-idle, /etc/systemd/system/hytale-idle.service, and it enables that
# service. Your Hytale container, its ports and its world are not touched.
set -euo pipefail

BIN=/usr/local/bin/hytale-idle
UNIT=/etc/systemd/system/hytale-idle.service
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

container="" idle="" status_http="-" yes=0 dry=0 uninstall=0
while [ $# -gt 0 ]; do
    case "$1" in
        --container) container="$2"; shift 2 ;;
        --idle-minutes) idle="$2"; shift 2 ;;
        --status-http) status_http="$2"; shift 2 ;;
        --yes|-y) yes=1; shift ;;
        --dry-run) dry=1; shift ;;
        --uninstall) uninstall=1; shift ;;
        -h|--help) sed -n '2,10p' "$0"; exit 0 ;;
        *) echo "unknown option: $1 (see --help)" >&2; exit 2 ;;
    esac
done

say() { printf '%s\n' "$*"; }
run() { if [ "$dry" = 1 ]; then say "  [dry-run] $*"; else "$@"; fi; }
ask() { # ask "question" default -> answer (the default when --yes or no terminal)
    local q="$1" d="$2" a=""
    if [ "$yes" = 1 ] || [ ! -t 0 ]; then printf '%s\n' "$d"; return; fi
    read -r -p "$q [$d]: " a </dev/tty || true
    printf '%s\n' "${a:-$d}"
}

if [ "$dry" = 0 ] && [ "$(id -u)" != 0 ]; then say "Run it as root (sudo ./install.sh), or try --dry-run first."; exit 1; fi

if [ "$uninstall" = 1 ]; then
    say "Removing hytale-idle. Your Hytale container is left exactly as it is (if it is asleep, start it with: docker start <name>)."
    run systemctl disable --now hytale-idle 2>/dev/null || true
    run rm -f "$UNIT" "$BIN"
    run systemctl daemon-reload
    say "Done. The status file in /var/lib/hytale-idle was kept; delete it if you like."
    exit 0
fi

for cmd in docker python3 systemctl; do
    command -v "$cmd" >/dev/null || { say "Needs $cmd, which is not installed."; exit 1; }
done

# Containers that publish a UDP port (Hytale listens on UDP, default 5520).
candidates=()
while read -r name; do
    [ -n "$name" ] || continue
    if docker inspect "$name" --format '{{json .HostConfig.PortBindings}}' 2>/dev/null | grep -q '/udp'; then candidates+=("$name"); fi
done < <(docker ps -a --format '{{.Names}}')

if [ -z "$container" ]; then
    if [ "${#candidates[@]}" = 0 ]; then
        say "No Docker container publishes a UDP port, so there is nothing to wake. Start your Hytale container"
        say "with its port published (for example -p 5520:5520/udp), then run this again."
        exit 1
    fi
    guess="${candidates[0]}"
    for c in "${candidates[@]}"; do case "$c" in *hytale*|*Hytale*) guess="$c"; break ;; esac; done
    say "Containers with a published UDP port: ${candidates[*]}"
    container="$(ask "Which one is your Hytale server?" "$guess")"
fi
docker inspect "$container" >/dev/null 2>&1 || { say "No container named $container."; exit 1; }
ports="$(docker inspect "$container" --format '{{json .HostConfig.PortBindings}}' | python3 -c 'import json,sys; print(" ".join(sorted({b["HostPort"] for k,v in (json.load(sys.stdin) or {}).items() if k.endswith("/udp") for b in (v or []) if b.get("HostPort")})))')"
[ -n "$ports" ] || { say "$container publishes no UDP port; hytale-idle wakes it by listening there."; exit 1; }

[ -n "$idle" ] || idle="$(ask "Stop it after how many minutes with nobody playing?" 15)"
case "$idle" in ''|*[!0-9.]*) say "Minutes must be a number."; exit 1 ;; esac
if [ "$status_http" = "-" ]; then
    status_http="$(ask "Serve its status over HTTP for dashboards or AI agents? Address, or 'off'" off)"
fi
[ "$status_http" = "off" ] && status_http=""

args="--container $container --idle-minutes $idle"
[ -n "$status_http" ] && args="$args --status-http $status_http"

say ""
say "About to set up:"
say "  watch $container (UDP $ports): start it on a join attempt, stop it after $idle min with nobody playing"
say "  status: hytale-idle status${status_http:+, and http://$status_http/status.json}"
say "  files: $BIN and $UNIT"
if [ "$yes" = 0 ] && [ -t 0 ] && [ "$dry" = 0 ]; then
    a="$(ask "Go ahead? (y/n)" y)"; case "$a" in y|Y|yes) ;; *) say "Nothing changed."; exit 0 ;; esac
fi

run install -m 755 "$HERE/hytale_idle.py" "$BIN"
unit="[Unit]
Description=Start Hytale on a join attempt, stop it when nobody plays (hytale-idle)
After=docker.service
Requires=docker.service

[Service]
ExecStart=/usr/bin/python3 $BIN $args
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target"
if [ "$dry" = 1 ]; then say "  [dry-run] write $UNIT:"; printf '%s\n' "$unit" | sed 's/^/      /'; else printf '%s\n' "$unit" > "$UNIT"; fi
run systemctl daemon-reload
run systemctl enable --now hytale-idle

say ""
say "Done. Check it with:  hytale-idle status   (logs: journalctl -u hytale-idle -f)"
say "The first player to join a sleeping server waits about a minute and joins again; tell your players."
