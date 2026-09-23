#!/usr/bin/env bash
set -euo pipefail

APP_NAME="tg-codex-agent"
REPO_APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

LIVE_DIR="/ops/apps/${APP_NAME}"
CONF_DIR="/ops/conf/${APP_NAME}"
ENV_FILE="${CONF_DIR}/.env"

RUNNER_USER="codex-runner"
RUNNER_HOME="/home/${RUNNER_USER}"

SERVICE_NAME="${APP_NAME}.service"
SERVICE_SOURCE="${REPO_APP_DIR}/deploy/${SERVICE_NAME}"
SERVICE_TARGET="/etc/systemd/system/${SERVICE_NAME}"

log() {
    echo "[INFO] $*"
}

warn() {
    echo "[WARN] $*" >&2
}

fail() {
    echo "[ERROR] $*" >&2
    exit 1
}

require_root() {
    if [ "$(id -u)" -ne 0 ]; then
        fail "Please run as root."
    fi
}

check_system() {
    if [ ! -f /etc/debian_version ]; then
        fail "Only Ubuntu/Debian is supported."
    fi
}

install_system_packages() {
    log "Installing system packages..."

    apt-get update

    DEBIAN_FRONTEND=noninteractive \
        apt-get install -y \
        python3 \
        python3-venv \
        python3-pip \
        git \
        curl \
        rsync \
        bubblewrap \
        ca-certificates
}

create_runner() {
    if id "${RUNNER_USER}" >/dev/null 2>&1; then
        log "Runner user already exists: ${RUNNER_USER}"
    else
        log "Creating restricted runner user: ${RUNNER_USER}"

        useradd \
            --create-home \
            --home-dir "${RUNNER_HOME}" \
            --shell /bin/bash \
            "${RUNNER_USER}"
    fi

    chmod 755 "${RUNNER_HOME}"
}

create_layout() {
    log "Creating application directories..."

    mkdir -p \
        /ops/apps \
        /ops/conf \
        /ops/script \
        /ops/logs \
        /ops/backup

    chmod 755 \
        /ops \
        /ops/apps \
        /ops/conf \
        /ops/script \
        /ops/logs \
        /ops/backup

    mkdir -p \
        "${LIVE_DIR}/app" \
        "${LIVE_DIR}/data/tasks" \
        "${LIVE_DIR}/data/snapshots" \
        "${LIVE_DIR}/logs" \
        "${CONF_DIR}"

    chmod 755 \
        "${LIVE_DIR}" \
        "${LIVE_DIR}/app"

    chmod 700 \
        "${LIVE_DIR}/data" \
        "${LIVE_DIR}/data/tasks" \
        "${LIVE_DIR}/data/snapshots" \
        "${CONF_DIR}"

    chmod 755 "${LIVE_DIR}/logs"
}

install_application() {
    log "Installing application source..."

    install -m 0644 \
        "${REPO_APP_DIR}/app/bot.py" \
        "${LIVE_DIR}/app/bot.py"

    install -m 0644 \
        "${REPO_APP_DIR}/app/codex_runner.py" \
        "${LIVE_DIR}/app/codex_runner.py"

    install -m 0644 \
        "${REPO_APP_DIR}/app/quota.py" \
        "${LIVE_DIR}/app/quota.py"

    install -m 0644 \
        "${REPO_APP_DIR}/app/task_manager.py" \
        "${LIVE_DIR}/app/task_manager.py"

    install -m 0644 \
        "${REPO_APP_DIR}/requirements.txt" \
        "${LIVE_DIR}/requirements.txt"
}

install_python_environment() {
    log "Creating Python virtual environment..."

    if [ ! -x "${LIVE_DIR}/venv/bin/python" ]; then
        python3 -m venv "${LIVE_DIR}/venv"
    fi

    "${LIVE_DIR}/venv/bin/python" \
        -m pip install \
        --upgrade pip

    "${LIVE_DIR}/venv/bin/python" \
        -m pip install \
        -r "${LIVE_DIR}/requirements.txt"
}

prepare_configuration() {
    if [ -f "${ENV_FILE}" ]; then
        log "Existing .env preserved."
        chmod 600 "${ENV_FILE}"
        return
    fi

    log "Creating configuration template."

    install -m 0600 \
        "${REPO_APP_DIR}/.env.example" \
        "${ENV_FILE}"

    warn "Edit ${ENV_FILE} and set TG_BOT_TOKEN / TG_ALLOWED_USER_IDS before starting the service."
}

install_service() {
    log "Installing systemd service..."

    install -m 0644 \
        "${SERVICE_SOURCE}" \
        "${SERVICE_TARGET}"

    systemctl daemon-reload
    systemctl enable "${SERVICE_NAME}"
}

check_codex() {
    local runner_codex="${RUNNER_HOME}/.local/bin/codex"
    local root_codex="/root/.local/bin/codex"

    if [ ! -x "${runner_codex}" ]; then
        warn "Runner Codex CLI is not installed:"
        warn "  ${runner_codex}"
        warn "Install Codex CLI and authenticate as ${RUNNER_USER}."
    else
        log "Runner Codex CLI found."
    fi

    if [ ! -x "${root_codex}" ]; then
        warn "Root Codex CLI is not installed:"
        warn "  ${root_codex}"
        warn "Root Codex is currently used by quota inspection."
    else
        log "Root Codex CLI found."
    fi
}

finish() {
    echo
    log "Installation files are ready."
    echo
    echo "Manual recovery items still required:"
    echo "  1. Configure ${ENV_FILE}"
    echo "  2. Install/login Codex for ${RUNNER_USER}"
    echo "  3. Install/login Codex for root if quota inspection is required"
    echo "  4. Restore/configure root GitHub SSH credentials"
    echo "  5. Verify Git remote access"
    echo
    echo "After those steps:"
    echo "  systemctl restart ${SERVICE_NAME}"
    echo "  ${REPO_APP_DIR}/deploy/health-check.sh"
}

main() {
    require_root
    check_system
    install_system_packages
    create_runner
    create_layout
    install_application
    install_python_environment
    prepare_configuration
    install_service
    check_codex
    finish
}

main "$@"
