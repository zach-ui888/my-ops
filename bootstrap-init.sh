#!/usr/bin/env bash
set -euo pipefail

TIMEZONE="Asia/Shanghai"
OPS_DIR="/ops"
OPS_SUBDIRS=(
  apps
  conf
  script
  logs
  backup
)

log() {
  echo "[INFO] $*"
}

fail() {
  echo "[ERROR] $*" >&2
  exit 1
}

require_root() {
  if [ "$(id -u)" -ne 0 ]; then
    fail "请使用 root 执行，或用 sudo bash 运行"
  fi
}

check_system() {
  if [ ! -f /etc/debian_version ]; then
    fail "仅支持 Ubuntu / Debian 系统"
  fi
}

set_timezone() {
  log "设置系统时区为 ${TIMEZONE}"

  if [ ! -e "/usr/share/zoneinfo/${TIMEZONE}" ]; then
    fail "时区文件不存在: /usr/share/zoneinfo/${TIMEZONE}"
  fi

  if command -v timedatectl >/dev/null 2>&1; then
    timedatectl set-timezone "${TIMEZONE}"
  else
    ln -snf "/usr/share/zoneinfo/${TIMEZONE}" /etc/localtime
    echo "${TIMEZONE}" > /etc/timezone
  fi

  log "当前系统时间: $(date '+%F %T %Z')"
}

create_ops_layout() {
  log "创建基础目录结构: ${OPS_DIR}"

  mkdir -p "${OPS_DIR}"
  chmod 755 "${OPS_DIR}"

  for dir in "${OPS_SUBDIRS[@]}"; do
    mkdir -p "${OPS_DIR}/${dir}"
    chmod 755 "${OPS_DIR}/${dir}"
  done

  log "目录结构创建完成"
}

show_ops_layout() {
  log "当前目录状态:"
  ls -ld "${OPS_DIR}" "${OPS_DIR}"/* 2>/dev/null
}

main() {
  require_root
  check_system
  set_timezone
  create_ops_layout
  show_ops_layout
  log "初始化完成"
}

main "$@"
