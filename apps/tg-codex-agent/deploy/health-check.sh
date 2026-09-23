#!/usr/bin/env bash
set -u

APP_NAME="tg-codex-agent"
LIVE_DIR="/ops/apps/${APP_NAME}"
CONF_DIR="/ops/conf/${APP_NAME}"
ENV_FILE="${CONF_DIR}/.env"

RUNNER_USER="codex-runner"
RUNNER_CODEX="/home/${RUNNER_USER}/.local/bin/codex"
ROOT_CODEX="/root/.local/bin/codex"

FAILED=0

ok() {
    echo "[OK]   $*"
}

fail() {
    echo "[FAIL] $*" >&2
    FAILED=1
}

check_file() {
    if [ -f "$1" ]; then
        ok "File exists: $1"
    else
        fail "Missing file: $1"
    fi
}

check_dir() {
    if [ -d "$1" ]; then
        ok "Directory exists: $1"
    else
        fail "Missing directory: $1"
    fi
}

echo "=== TG-Codex Agent Health Check ==="

check_dir "${LIVE_DIR}"
check_dir "${LIVE_DIR}/app"
check_dir "${LIVE_DIR}/data/tasks"
check_dir "${LIVE_DIR}/data/snapshots"
check_dir "${CONF_DIR}"

check_file "${LIVE_DIR}/app/bot.py"
check_file "${LIVE_DIR}/app/codex_runner.py"
check_file "${LIVE_DIR}/app/quota.py"
check_file "${LIVE_DIR}/app/task_manager.py"
check_file "${ENV_FILE}"

if id "${RUNNER_USER}" >/dev/null 2>&1; then
    ok "Runner user exists: ${RUNNER_USER}"
else
    fail "Runner user missing: ${RUNNER_USER}"
fi

if [ -x "${LIVE_DIR}/venv/bin/python" ]; then
    ok "Python venv exists"

    if "${LIVE_DIR}/venv/bin/python" -m py_compile \
        "${LIVE_DIR}/app/bot.py" \
        "${LIVE_DIR}/app/codex_runner.py" \
        "${LIVE_DIR}/app/quota.py" \
        "${LIVE_DIR}/app/task_manager.py"
    then
        ok "Python source compile check"
    else
        fail "Python source compile check"
    fi
else
    fail "Python venv missing"
fi

if [ -x "${RUNNER_CODEX}" ]; then
    ok "Runner Codex CLI exists"
else
    fail "Runner Codex CLI missing"
fi

if [ -x "${ROOT_CODEX}" ]; then
    ok "Root Codex CLI exists"
else
    fail "Root Codex CLI missing"
fi

if systemctl is-enabled "${APP_NAME}" >/dev/null 2>&1; then
    ok "Service enabled"
else
    fail "Service not enabled"
fi

if systemctl is-active "${APP_NAME}" >/dev/null 2>&1; then
    ok "Service active"
else
    fail "Service not active"
fi

if [ -d /root/my-ops/.git ]; then
    if git -C /root/my-ops remote get-url origin >/dev/null 2>&1; then
        ok "Git origin configured"
    else
        fail "Git origin missing"
    fi
fi

echo

if [ "${FAILED}" -eq 0 ]; then
    echo "RESULT: HEALTHY"
    exit 0
fi

echo "RESULT: CHECK FAILED"
exit 1
