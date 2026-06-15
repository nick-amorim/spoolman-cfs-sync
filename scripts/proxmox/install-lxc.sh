#!/usr/bin/env bash
set -Eeuo pipefail

APP_NAME="spoolman-cfs-sync"
DEFAULT_REPO="https://github.com/nick-amorim/spoolman-cfs-sync.git"
DEFAULT_BRANCH="main"
DEFAULT_HOSTNAME="spoolman-cfs-sync"
DEFAULT_CORES="1"
DEFAULT_MEMORY="512"
DEFAULT_SWAP="512"
DEFAULT_DISK="4"
DEFAULT_TEMPLATE_STORAGE="local"
DEFAULT_STORAGE="local-lvm"
DEFAULT_BRIDGE="vmbr0"
DEFAULT_IP_CONFIG="dhcp"
DEFAULT_PORT="8005"

CTID=""
HOSTNAME="$DEFAULT_HOSTNAME"
CORES="$DEFAULT_CORES"
MEMORY="$DEFAULT_MEMORY"
SWAP="$DEFAULT_SWAP"
DISK_SIZE="$DEFAULT_DISK"
TEMPLATE_STORAGE="$DEFAULT_TEMPLATE_STORAGE"
STORAGE="$DEFAULT_STORAGE"
BRIDGE="$DEFAULT_BRIDGE"
IP_CONFIG="$DEFAULT_IP_CONFIG"
REPO_URL="$DEFAULT_REPO"
BRANCH="$DEFAULT_BRANCH"
APP_PORT="$DEFAULT_PORT"
MOONRAKER_URL=""
SPOOLMAN_URL=""
SYNC_MODE="live"
UPDATE_CTID=""
ADVANCED=0
DEFAULT_MODE=0
CUSTOM_OPTIONS=0

info() { echo -e "\033[1;34m[INFO]\033[0m $*"; }
ok() { echo -e "\033[1;32m[OK]\033[0m $*"; }
warn() { echo -e "\033[1;33m[WARN]\033[0m $*"; }
die() { echo -e "\033[1;31m[ERROR]\033[0m $*" >&2; exit 1; }

usage() {
  cat <<EOF
Usage:
  bash install-lxc.sh [options]
  bash install-lxc.sh --update <ctid>

Create a Proxmox LXC for ${APP_NAME}.

Options:
  --ctid <id>                  Container id. Defaults to next available id.
  --hostname <name>            Container hostname. Default: ${DEFAULT_HOSTNAME}
  --storage <name>             Root disk storage. Default: ${DEFAULT_STORAGE}
  --template-storage <name>    Template storage. Default: ${DEFAULT_TEMPLATE_STORAGE}
  --bridge <name>              Network bridge. Default: ${DEFAULT_BRIDGE}
  --ip <config>                LXC IP config, e.g. dhcp or 192.168.1.50/24. Default: dhcp
  --cores <n>                  CPU cores. Default: ${DEFAULT_CORES}
  --memory <mb>                RAM in MB. Default: ${DEFAULT_MEMORY}
  --swap <mb>                  Swap in MB. Default: ${DEFAULT_SWAP}
  --disk <gb>                  Root disk size in GB. Default: ${DEFAULT_DISK}
  --repo <url>                 Git repository. Default: ${DEFAULT_REPO}
  --branch <name>              Git branch/tag to deploy. Default: ${DEFAULT_BRANCH}
  --port <port>                App port inside the LXC. Default: ${DEFAULT_PORT}
  --moonraker-url <url>        Optional initial Moonraker URL.
  --spoolman-url <url>         Optional initial Spoolman URL.
  --sync-mode <mode>           post_print or live. Default: live.
  --default                    Use default install settings without prompting.
  --advanced                   Prompt for common settings.
  --update <ctid>              Run the in-container updater for an existing install.
  -h, --help                   Show this help.

Examples:
  bash <(curl -fsSL https://raw.githubusercontent.com/nick-amorim/spoolman-cfs-sync/main/scripts/proxmox/install-lxc.sh)

  bash <(curl -fsSL https://raw.githubusercontent.com/nick-amorim/spoolman-cfs-sync/main/scripts/proxmox/install-lxc.sh) \\
    --ctid 120 --storage local-lvm --bridge vmbr0 \\
    --moonraker-url http://192.168.1.12:7125 \\
    --spoolman-url http://192.168.1.72:7912

  bash <(curl -fsSL https://raw.githubusercontent.com/nick-amorim/spoolman-cfs-sync/main/scripts/proxmox/install-lxc.sh) --update 120
EOF
}

need_root() {
  [[ "${EUID}" -eq 0 ]] || die "Run this from the Proxmox host root shell."
}

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || die "Missing required command: $1"
}

parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --ctid) CTID="${2:?}"; CUSTOM_OPTIONS=1; shift 2 ;;
      --hostname) HOSTNAME="${2:?}"; CUSTOM_OPTIONS=1; shift 2 ;;
      --storage) STORAGE="${2:?}"; CUSTOM_OPTIONS=1; shift 2 ;;
      --template-storage) TEMPLATE_STORAGE="${2:?}"; CUSTOM_OPTIONS=1; shift 2 ;;
      --bridge) BRIDGE="${2:?}"; CUSTOM_OPTIONS=1; shift 2 ;;
      --ip) IP_CONFIG="${2:?}"; CUSTOM_OPTIONS=1; shift 2 ;;
      --cores) CORES="${2:?}"; CUSTOM_OPTIONS=1; shift 2 ;;
      --memory) MEMORY="${2:?}"; CUSTOM_OPTIONS=1; shift 2 ;;
      --swap) SWAP="${2:?}"; CUSTOM_OPTIONS=1; shift 2 ;;
      --disk) DISK_SIZE="${2:?}"; CUSTOM_OPTIONS=1; shift 2 ;;
      --repo) REPO_URL="${2:?}"; CUSTOM_OPTIONS=1; shift 2 ;;
      --branch) BRANCH="${2:?}"; CUSTOM_OPTIONS=1; shift 2 ;;
      --port) APP_PORT="${2:?}"; CUSTOM_OPTIONS=1; shift 2 ;;
      --moonraker-url) MOONRAKER_URL="${2:?}"; CUSTOM_OPTIONS=1; shift 2 ;;
      --spoolman-url) SPOOLMAN_URL="${2:?}"; CUSTOM_OPTIONS=1; shift 2 ;;
      --sync-mode) SYNC_MODE="${2:?}"; CUSTOM_OPTIONS=1; shift 2 ;;
      --default) DEFAULT_MODE=1; shift ;;
      --advanced) ADVANCED=1; shift ;;
      --update) UPDATE_CTID="${2:?}"; shift 2 ;;
      -h|--help) usage; exit 0 ;;
      *) die "Unknown option: $1" ;;
    esac
  done
}

prompt_install_mode() {
  [[ "$ADVANCED" -eq 0 ]] || return 0
  [[ "$DEFAULT_MODE" -eq 0 ]] || return 0
  [[ "$CUSTOM_OPTIONS" -eq 0 ]] || return 0
  [[ -t 0 ]] || return 0

  local suggested_ctid
  suggested_ctid="$(next_ctid)"

  echo
  echo "${APP_NAME} LXC installer"
  echo
  echo "Default settings:"
  echo "  CTID:             ${suggested_ctid}"
  echo "  Hostname:         ${HOSTNAME}"
  echo "  CPU cores:        ${CORES}"
  echo "  Memory:           ${MEMORY} MB"
  echo "  Swap:             ${SWAP} MB"
  echo "  Disk:             ${DISK_SIZE} GB"
  echo "  Storage:          ${STORAGE}"
  echo "  Template storage: ${TEMPLATE_STORAGE}"
  echo "  Network:          ${BRIDGE}, ${IP_CONFIG}"
  echo "  App port:         ${APP_PORT}"
  echo

  local answer
  read -r -p "Use default settings? [Y/n]: " answer
  case "${answer,,}" in
    n|no)
      CTID="$suggested_ctid"
      ADVANCED=1
      ;;
    *)
      CTID="$suggested_ctid"
      ;;
  esac
}

prompt_if_advanced() {
  [[ "$ADVANCED" -eq 1 ]] || return 0
  local value
  local suggested_ctid="${CTID:-$(next_ctid)}"
  echo
  echo "Advanced settings. Press Enter to accept the suggested value."
  read -r -p "CTID [${suggested_ctid}]: " value; CTID="${value:-$suggested_ctid}"
  read -r -p "Hostname [${HOSTNAME}]: " value; HOSTNAME="${value:-$HOSTNAME}"
  read -r -p "Storage [${STORAGE}]: " value; STORAGE="${value:-$STORAGE}"
  read -r -p "Template storage [${TEMPLATE_STORAGE}]: " value; TEMPLATE_STORAGE="${value:-$TEMPLATE_STORAGE}"
  read -r -p "Bridge [${BRIDGE}]: " value; BRIDGE="${value:-$BRIDGE}"
  read -r -p "IP config [${IP_CONFIG}]: " value; IP_CONFIG="${value:-$IP_CONFIG}"
  read -r -p "CPU cores [${CORES}]: " value; CORES="${value:-$CORES}"
  read -r -p "Memory MB [${MEMORY}]: " value; MEMORY="${value:-$MEMORY}"
  read -r -p "Disk GB [${DISK_SIZE}]: " value; DISK_SIZE="${value:-$DISK_SIZE}"
  read -r -p "App port [${APP_PORT}]: " value; APP_PORT="${value:-$APP_PORT}"
  read -r -p "Moonraker URL [${MOONRAKER_URL:-blank}]: " value; MOONRAKER_URL="${value:-$MOONRAKER_URL}"
  read -r -p "Spoolman URL [${SPOOLMAN_URL:-blank}]: " value; SPOOLMAN_URL="${value:-$SPOOLMAN_URL}"
  read -r -p "Sync mode [${SYNC_MODE}]: " value; SYNC_MODE="${value:-$SYNC_MODE}"
}

next_ctid() {
  if command -v pvesh >/dev/null 2>&1; then
    pvesh get /cluster/nextid
  else
    for id in $(seq 100 999); do
      pct status "$id" >/dev/null 2>&1 || { echo "$id"; return; }
    done
    die "Unable to find a free CTID."
  fi
}

latest_debian_template() {
  pveam update >/dev/null
  pveam available --section system \
    | awk '/debian-12-standard/ {print $2}' \
    | sort -V \
    | tail -n 1
}

download_template_if_needed() {
  local template="$1"
  pveam list "$TEMPLATE_STORAGE" | awk '{print $1}' | grep -qx "${TEMPLATE_STORAGE}:vztmpl/${template}" && return 0
  info "Downloading template ${template} to ${TEMPLATE_STORAGE}"
  pveam download "$TEMPLATE_STORAGE" "$template"
}

container_ip() {
  pct exec "$CTID" -- bash -lc "hostname -I 2>/dev/null | awk '{print \$1}'" || true
}

wait_for_network() {
  info "Waiting for container network"
  for _ in $(seq 1 60); do
    if pct exec "$CTID" -- bash -lc "getent hosts github.com >/dev/null 2>&1"; then
      return 0
    fi
    sleep 2
  done
  die "Container network did not become ready."
}

run_update() {
  [[ -n "$UPDATE_CTID" ]] || return 1
  pct status "$UPDATE_CTID" >/dev/null 2>&1 || die "Container ${UPDATE_CTID} does not exist."
  info "Running ${APP_NAME} updater in CT ${UPDATE_CTID}"
  pct exec "$UPDATE_CTID" -- bash -lc "if command -v update >/dev/null 2>&1; then update; else spoolman-cfs-sync-update; fi"
  ok "Updated ${APP_NAME} in CT ${UPDATE_CTID}"
  exit 0
}

create_container() {
  CTID="${CTID:-$(next_ctid)}"
  pct status "$CTID" >/dev/null 2>&1 && die "CTID ${CTID} already exists."

  local template
  template="$(latest_debian_template)"
  [[ -n "$template" ]] || die "Could not find a Debian 12 LXC template."
  download_template_if_needed "$template"

  local rootfs="${STORAGE}:${DISK_SIZE}"
  local net0="name=eth0,bridge=${BRIDGE},ip=${IP_CONFIG}"
  local password
  password="$(openssl rand -base64 24 | tr -d '\n')"

  info "Creating CT ${CTID} (${HOSTNAME})"
  pct create "$CTID" "${TEMPLATE_STORAGE}:vztmpl/${template}" \
    --hostname "$HOSTNAME" \
    --ostype debian \
    --unprivileged 1 \
    --cores "$CORES" \
    --memory "$MEMORY" \
    --swap "$SWAP" \
    --rootfs "$rootfs" \
    --net0 "$net0" \
    --features nesting=1 \
    --onboot 1 \
    --password "$password" \
    --tags "spoolman-cfs-sync;filament;3d-print" \
    --description "spoolman-cfs-sync"

  info "Starting CT ${CTID}"
  pct start "$CTID"
  wait_for_network
}

install_app() {
  local raw_base="https://raw.githubusercontent.com/nick-amorim/spoolman-cfs-sync/${BRANCH}/scripts/proxmox"
  local args=(--repo "$REPO_URL" --branch "$BRANCH" --port "$APP_PORT" --sync-mode "$SYNC_MODE")
  [[ -n "$MOONRAKER_URL" ]] && args+=(--moonraker-url "$MOONRAKER_URL")
  [[ -n "$SPOOLMAN_URL" ]] && args+=(--spoolman-url "$SPOOLMAN_URL")

  info "Bootstrapping installer dependencies in CT ${CTID}"
  pct exec "$CTID" -- bash -lc "export DEBIAN_FRONTEND=noninteractive; apt-get update >/dev/null && apt-get install -y --no-install-recommends ca-certificates curl >/dev/null"

  info "Installing ${APP_NAME} in CT ${CTID}"
  pct exec "$CTID" -- bash -lc "curl -fsSL '${raw_base}/install-app.sh' -o /tmp/spoolman-cfs-sync-install-app.sh"
  pct exec "$CTID" -- bash /tmp/spoolman-cfs-sync-install-app.sh "${args[@]}"
}

main() {
  parse_args "$@"
  need_root
  need_cmd pct
  need_cmd pveam
  need_cmd curl
  need_cmd openssl
  run_update || true
  prompt_install_mode
  prompt_if_advanced
  [[ "$SYNC_MODE" == "live" || "$SYNC_MODE" == "post_print" ]] || die "--sync-mode must be live or post_print"
  create_container
  install_app

  local ip
  ip="$(container_ip)"
  ok "${APP_NAME} LXC created successfully."
  echo
  echo "Container: ${CTID}"
  echo "Service:   systemctl status spoolman-cfs-sync"
  echo "Update:    update"
  if [[ -n "$ip" ]]; then
    echo "URL:       http://${ip}:${APP_PORT}"
  else
    echo "URL:       http://<container-ip>:${APP_PORT}"
  fi
}

main "$@"
