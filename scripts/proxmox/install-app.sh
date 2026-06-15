#!/usr/bin/env bash
set -Eeuo pipefail

APP_NAME="spoolman-cfs-sync"
SERVICE_NAME="spoolman-cfs-sync"
APP_DIR="/opt/spoolman-cfs-sync"
APP_USER="spoolman-cfs-sync"
REPO_URL="https://github.com/nick-amorim/spoolman-cfs-sync.git"
BRANCH="main"
APP_PORT="8005"
MOONRAKER_URL=""
SPOOLMAN_URL=""
SYNC_MODE="live"
LOG_FILE="${TMPDIR:-/tmp}/${APP_NAME}-app-install-$(date +%Y%m%d-%H%M%S).log"

info() { echo -e "\033[1;34m[INFO]\033[0m $*"; }
ok() { echo -e "\033[1;32m[OK]\033[0m $*"; }
die() { echo -e "\033[1;31m[ERROR]\033[0m $*" >&2; exit 1; }

run_quiet() {
  local label="$1"
  shift
  info "$label"
  echo "## ${label}" >>"$LOG_FILE"
  if "$@" >>"$LOG_FILE" 2>&1; then
    ok "$label"
    return 0
  fi

  echo -e "\033[1;31m[ERROR]\033[0m ${label} failed" >&2
  echo "Last log lines from ${LOG_FILE}:" >&2
  tail -n 40 "$LOG_FILE" >&2 || true
  exit 1
}

usage() {
  cat <<EOF
Usage: install-app.sh [options]

Installs ${APP_NAME} inside a Debian LXC.

Options:
  --repo <url>           Git repository. Default: ${REPO_URL}
  --branch <name>        Git branch/tag. Default: ${BRANCH}
  --app-dir <path>       Install directory. Default: ${APP_DIR}
  --user <name>          System user. Default: ${APP_USER}
  --port <port>          App port. Default: ${APP_PORT}
  --moonraker-url <url>  Optional initial Moonraker URL.
  --spoolman-url <url>   Optional initial Spoolman URL.
  --sync-mode <mode>     live or post_print. Default: live.
  -h, --help             Show this help.
EOF
}

parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --repo) REPO_URL="${2:?}"; shift 2 ;;
      --branch) BRANCH="${2:?}"; shift 2 ;;
      --app-dir) APP_DIR="${2:?}"; shift 2 ;;
      --user) APP_USER="${2:?}"; shift 2 ;;
      --port) APP_PORT="${2:?}"; shift 2 ;;
      --moonraker-url) MOONRAKER_URL="${2:?}"; shift 2 ;;
      --spoolman-url) SPOOLMAN_URL="${2:?}"; shift 2 ;;
      --sync-mode) SYNC_MODE="${2:?}"; shift 2 ;;
      -h|--help) usage; exit 0 ;;
      *) die "Unknown option: $1" ;;
    esac
  done
}

need_root() {
  [[ "${EUID}" -eq 0 ]] || die "Run this script as root inside the LXC."
}

install_packages() {
  export DEBIAN_FRONTEND=noninteractive
  export LC_ALL=C
  export LANG=C
  run_quiet "Updating package indexes" apt-get update -qq
  run_quiet "Installing system packages" apt-get install -y -qq --no-install-recommends \
    ca-certificates \
    curl \
    git \
    python3 \
    python3-pip \
    python3-venv \
    python3-dev \
    build-essential
}

create_user() {
  if ! id "$APP_USER" >/dev/null 2>&1; then
    info "Creating system user ${APP_USER}"
    useradd --system --create-home --home-dir "$APP_DIR" --shell /usr/sbin/nologin "$APP_USER"
  fi
}

clone_or_update_repo() {
  if [[ -d "${APP_DIR}/.git" ]]; then
    run_quiet "Trusting app repository for Git" git config --global --add safe.directory "$APP_DIR"
    run_quiet "Fetching repository updates" git -C "$APP_DIR" fetch --prune origin
    run_quiet "Checking out ${BRANCH}" git -C "$APP_DIR" checkout "$BRANCH"
    run_quiet "Resetting app to origin/${BRANCH}" git -C "$APP_DIR" reset --hard "origin/${BRANCH}"
  else
    rm -rf "$APP_DIR"
    run_quiet "Cloning repository" git clone --quiet --depth 1 --branch "$BRANCH" "$REPO_URL" "$APP_DIR"
  fi
  chown -R "$APP_USER:$APP_USER" "$APP_DIR"
}

install_python_deps() {
  run_quiet "Creating Python virtual environment" python3 -m venv "${APP_DIR}/.venv"
  run_quiet "Upgrading pip" "${APP_DIR}/.venv/bin/python" -m pip install --quiet --upgrade pip
  run_quiet "Installing Python dependencies" "${APP_DIR}/.venv/bin/python" -m pip install --quiet -r "${APP_DIR}/requirements.txt"
  chown -R "$APP_USER:$APP_USER" "${APP_DIR}/.venv"
}

write_initial_config() {
  mkdir -p "${APP_DIR}/data"
  if [[ -f "${APP_DIR}/data/config.json" ]]; then
    chown -R "$APP_USER:$APP_USER" "${APP_DIR}/data"
    return 0
  fi

  info "Writing initial config"
  cat >"${APP_DIR}/data/config.json" <<EOF
{
  "moonraker_url": "${MOONRAKER_URL}",
  "poll_interval_sec": 5.0,
  "filament_diameter_mm": 1.75,
  "cfs_autosync": true,
  "spoolman": {
    "enabled": false,
    "dry_run": true,
    "url": "${SPOOLMAN_URL}",
    "sync_mode": "${SYNC_MODE}",
    "live_min_delta_mm": 100.0,
    "timeout_sec": 5.0,
    "slot_mappings": {
      "1A": null,
      "1B": null,
      "1C": null,
      "1D": null,
      "2A": null,
      "2B": null,
      "2C": null,
      "2D": null,
      "3A": null,
      "3B": null,
      "3C": null,
      "3D": null,
      "4A": null,
      "4B": null,
      "4C": null,
      "4D": null
    }
  }
}
EOF
  chown -R "$APP_USER:$APP_USER" "${APP_DIR}/data"
}

write_service() {
  info "Creating systemd service"
  cat >"/etc/systemd/system/${SERVICE_NAME}.service" <<EOF
[Unit]
Description=spoolman-cfs-sync
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${APP_USER}
Group=${APP_USER}
WorkingDirectory=${APP_DIR}
ExecStart=${APP_DIR}/.venv/bin/uvicorn main:app --host 0.0.0.0 --port ${APP_PORT}
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
  run_quiet "Reloading systemd" systemctl daemon-reload
  run_quiet "Enabling and starting service" systemctl enable --now "$SERVICE_NAME"
  ok "Created systemd service"
}

write_env() {
  cat >"/etc/${SERVICE_NAME}.env" <<EOF
APP_DIR="${APP_DIR}"
APP_USER="${APP_USER}"
SERVICE_NAME="${SERVICE_NAME}"
REPO_URL="${REPO_URL}"
BRANCH="${BRANCH}"
APP_PORT="${APP_PORT}"
EOF
}

write_update_helper() {
  info "Installing update helper"
  cat >"/usr/local/bin/${SERVICE_NAME}-update" <<'EOF'
#!/usr/bin/env bash
set -Eeuo pipefail

LOG_FILE="${TMPDIR:-/tmp}/spoolman-cfs-sync-update-$(date +%Y%m%d-%H%M%S).log"
info() { echo -e "\033[1;34m[INFO]\033[0m $*"; }
ok() { echo -e "\033[1;32m[OK]\033[0m $*"; }
run_quiet() {
  local label="$1"
  shift
  info "$label"
  echo "## ${label}" >>"$LOG_FILE"
  if "$@" >>"$LOG_FILE" 2>&1; then
    ok "$label"
    return 0
  fi
  echo -e "\033[1;31m[ERROR]\033[0m ${label} failed" >&2
  echo "Last log lines from ${LOG_FILE}:" >&2
  tail -n 40 "$LOG_FILE" >&2 || true
  exit 1
}

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run as root: sudo spoolman-cfs-sync-update" >&2
  exit 1
fi

if [[ -f /etc/spoolman-cfs-sync.env ]]; then
  # shellcheck disable=SC1091
  source /etc/spoolman-cfs-sync.env
else
  APP_DIR="/opt/spoolman-cfs-sync"
  APP_USER="spoolman-cfs-sync"
  SERVICE_NAME="spoolman-cfs-sync"
  BRANCH="main"
fi

info "Updating ${SERVICE_NAME} from origin/${BRANCH}"
cd "$APP_DIR"
run_quiet "Trusting app repository for Git" git config --global --add safe.directory "$APP_DIR"
run_quiet "Fetching repository updates" git fetch --prune origin
run_quiet "Checking out ${BRANCH}" git checkout "$BRANCH"
run_quiet "Resetting app to origin/${BRANCH}" git reset --hard "origin/${BRANCH}"

run_quiet "Creating Python virtual environment" python3 -m venv "${APP_DIR}/.venv"
run_quiet "Upgrading pip" "${APP_DIR}/.venv/bin/python" -m pip install --quiet --upgrade pip
run_quiet "Installing Python dependencies" "${APP_DIR}/.venv/bin/python" -m pip install --quiet -r "${APP_DIR}/requirements.txt"
chown -R "${APP_USER}:${APP_USER}" "$APP_DIR"

run_quiet "Reloading systemd" systemctl daemon-reload
run_quiet "Restarting ${SERVICE_NAME}" systemctl restart "$SERVICE_NAME"
ok "Updated ${SERVICE_NAME}"
echo "Update log: ${LOG_FILE}"
EOF
  chmod +x "/usr/local/bin/${SERVICE_NAME}-update"
  cat >"/usr/local/bin/update" <<EOF
#!/usr/bin/env bash
set -Eeuo pipefail
exec /usr/local/bin/${SERVICE_NAME}-update "\$@"
EOF
  chmod +x "/usr/local/bin/update"
  ok "Installed update helper"
}

print_summary() {
  local ip
  ip="$(hostname -I 2>/dev/null | awk '{print $1}')"
  ok "${APP_NAME} installed successfully"
  echo
  echo "Service status: systemctl status ${SERVICE_NAME}"
  echo "Logs:           journalctl -u ${SERVICE_NAME} -f"
  echo "Update:         update"
  echo "Install log:    ${LOG_FILE}"
  if [[ -n "$ip" ]]; then
    echo "URL:            http://${ip}:${APP_PORT}"
  else
    echo "URL:            http://<container-ip>:${APP_PORT}"
  fi
  echo
  echo "Spoolman sync starts in dry-run mode. Open the UI, map your CFS slots, then enable writes."
}

main() {
  parse_args "$@"
  need_root
  [[ "$SYNC_MODE" == "live" || "$SYNC_MODE" == "post_print" ]] || die "--sync-mode must be live or post_print"
  install_packages
  create_user
  clone_or_update_repo
  install_python_deps
  write_initial_config
  write_service
  write_env
  write_update_helper
  print_summary
}

main "$@"
