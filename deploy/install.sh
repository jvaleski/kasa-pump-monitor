#!/usr/bin/env bash
#
# Install the plunge pump monitor as a systemd service on Linux.
#
#   ./deploy/install.sh                 install and start
#   ./deploy/install.sh --no-start      install the unit but don't start it
#   ./deploy/install.sh --uninstall     stop, disable, and remove the unit
#
#   Add --thermo to any of the above to act on the thermostat logger
#   (plunge-thermo, thermo_config.json) instead of the pump monitor.
#
# Run this from the directory the monitor will live in permanently; paths are
# baked into the unit file at install time.

set -euo pipefail

INSTALL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_DIR="${INSTALL_DIR}/.venv"

START_SERVICE=1
DO_UNINSTALL=0
TARGET="pump"
for arg in "$@"; do
    case "$arg" in
        --uninstall) DO_UNINSTALL=1 ;;
        --no-start)  START_SERVICE=0 ;;
        --thermo)    TARGET="thermo" ;;
        *)           echo "error: unknown option: $arg" >&2; exit 1 ;;
    esac
done

if [ "${TARGET}" = "thermo" ]; then
    SERVICE_NAME="plunge-thermo"
    CONFIG_NAME="thermo_config.json"
    EXAMPLE_NAME="thermo_config.example.json"
    APP_LOG="thermo.log"
else
    SERVICE_NAME="plunge-monitor"
    CONFIG_NAME="pump_config.json"
    EXAMPLE_NAME="pump_config.example.json"
    APP_LOG="pump_monitor.log"
fi
UNIT_PATH="/etc/systemd/system/${SERVICE_NAME}.service"
TEMPLATE="${INSTALL_DIR}/deploy/${SERVICE_NAME}.service.template"

die() { echo "error: $*" >&2; exit 1; }
info() { echo "==> $*"; }

need_sudo() {
    if [ "$(id -u)" -eq 0 ]; then SUDO=""; else
        command -v sudo >/dev/null 2>&1 || die "need root or sudo to manage systemd units"
        SUDO="sudo"
    fi
}

uninstall() {
    need_sudo
    info "Stopping and removing ${SERVICE_NAME}"
    $SUDO systemctl stop "${SERVICE_NAME}" 2>/dev/null || true
    $SUDO systemctl disable "${SERVICE_NAME}" 2>/dev/null || true
    $SUDO rm -f "${UNIT_PATH}"
    $SUDO systemctl daemon-reload
    echo "Removed. The install directory and its data were left alone."
    exit 0
}

[ "${DO_UNINSTALL}" -eq 1 ] && uninstall

# ---------------------------------------------------------------- preflight

[ "$(uname -s)" = "Linux" ] || die "this installer targets Linux/systemd; on macOS run 'pump_monitor.py watch' directly"
command -v systemctl >/dev/null 2>&1 || die "systemctl not found; this system doesn't use systemd"
[ -f "${TEMPLATE}" ] || die "missing ${TEMPLATE}"
[ -f "${INSTALL_DIR}/${CONFIG_NAME}" ] || die \
    "missing ${INSTALL_DIR}/${CONFIG_NAME} — copy it from the machine you set it up on, or start from ${EXAMPLE_NAME}"

command -v python3 >/dev/null 2>&1 || die "python3 not found"
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' \
    || die "python3 3.9+ required, found $(python3 --version)"

# ------------------------------------------------------------------- venv
# Debian/Raspberry Pi OS Bookworm and later mark the system Python as
# externally managed (PEP 668), so pip refuses to install into it. A venv
# sidesteps that and keeps this self-contained.

if [ ! -d "${VENV_DIR}" ]; then
    info "Creating virtualenv at ${VENV_DIR}"
    python3 -m venv "${VENV_DIR}" || die \
        "venv creation failed — on Debian/Raspberry Pi OS: sudo apt install python3-venv"
fi

info "Installing dependencies"
"${VENV_DIR}/bin/pip" install --quiet --upgrade pip
"${VENV_DIR}/bin/pip" install --quiet -r "${INSTALL_DIR}/requirements.txt"

# ------------------------------------------------------------ config check

chmod 600 "${INSTALL_DIR}/${CONFIG_NAME}"

if [ "${TARGET}" = "pump" ] && ! "${VENV_DIR}/bin/python3" - "$INSTALL_DIR" <<'PY'
import json, sys
path = f"{sys.argv[1]}/pump_config.json"
with open(path) as fh:
    cfg = json.load(fh)
if cfg.get("pump", {}).get("running_watts") is None:
    print(f"\n  {path} has no calibration (pump.running_watts is null).")
    print("  Run './pump_monitor.py calibrate' with the pump running before starting the service.\n")
    sys.exit(1)
PY
then
    die "config is not calibrated; refusing to install a service that would immediately fail"
fi

# ------------------------------------------------------------------- unit

RUN_USER="${SUDO_USER:-$(id -un)}"
RUN_GROUP="$(id -gn "${RUN_USER}")"

info "Writing ${UNIT_PATH} (user=${RUN_USER}, dir=${INSTALL_DIR})"
need_sudo
sed -e "s|__INSTALL_DIR__|${INSTALL_DIR}|g" \
    -e "s|__PYTHON__|${VENV_DIR}/bin/python3|g" \
    -e "s|__USER__|${RUN_USER}|g" \
    -e "s|__GROUP__|${RUN_GROUP}|g" \
    "${TEMPLATE}" | $SUDO tee "${UNIT_PATH}" >/dev/null

$SUDO systemctl daemon-reload
$SUDO systemctl enable "${SERVICE_NAME}" >/dev/null

if [ "${START_SERVICE}" -eq 1 ]; then
    info "Starting ${SERVICE_NAME}"
    $SUDO systemctl restart "${SERVICE_NAME}"
    sleep 3
    $SUDO systemctl --no-pager --lines=15 status "${SERVICE_NAME}" || true
else
    info "Installed but not started (--no-start)"
fi

cat <<EOF

Done.

  status   : systemctl status ${SERVICE_NAME}
  logs     : journalctl -u ${SERVICE_NAME} -f
  app log  : tail -f ${INSTALL_DIR}/${APP_LOG}
  restart  : sudo systemctl restart ${SERVICE_NAME}
  remove   : ./deploy/install.sh $([ "${TARGET}" = thermo ] && echo "--thermo ")--uninstall
EOF
