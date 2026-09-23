import json
import os
import subprocess
import time

from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo


# ============================================================
# 配置
# ============================================================

CODEX_HOME = Path(
    os.getenv(
        "CODEX_HOME",
        "/root/.codex",
    )
)

CODEX_BIN = os.getenv(
    "CODEX_BIN",
    "/root/.local/bin/codex",
)

CODEX_SESSIONS = CODEX_HOME / "sessions"

LOCAL_TZ = ZoneInfo(
    os.getenv(
        "TZ",
        "Asia/Shanghai",
    )
)

# 快照超过多少秒后，/usage 才主动刷新
# 默认 15 分钟
QUOTA_SNAPSHOT_MAX_AGE = int(
    os.getenv(
        "QUOTA_SNAPSHOT_MAX_AGE",
        "900",
    )
)

# Codex 刷新请求最长等待时间
CODEX_REFRESH_TIMEOUT = 60


# ============================================================
# Session 查找
# ============================================================

def find_latest_session():
    if not CODEX_SESSIONS.exists():
        return None

    files = list(
        CODEX_SESSIONS.rglob("*.jsonl")
    )

    if not files:
        return None

    return max(
        files,
        key=lambda p: p.stat().st_mtime,
    )


# ============================================================
# rate_limits 读取
# ============================================================

def get_latest_rate_limits():
    """
    从最新 Codex session 中读取最后一条
    包含 rate_limits 的记录。

    返回：
        rate_limits
        session_file
        snapshot_timestamp
    """

    session_file = find_latest_session()

    if session_file is None:
        raise RuntimeError(
            "未找到 Codex session 文件"
        )

    latest = None
    latest_timestamp = None

    with session_file.open(
        "r",
        encoding="utf-8",
    ) as f:

        for line in f:
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue

            payload = obj.get(
                "payload",
                {},
            )

            rate_limits = payload.get(
                "rate_limits"
            )

            if not rate_limits:
                continue

            latest = rate_limits

            # Codex JSONL 顶层通常带 timestamp
            timestamp_value = obj.get(
                "timestamp"
            )

            if timestamp_value:
                try:
                    dt = datetime.fromisoformat(
                        timestamp_value.replace(
                            "Z",
                            "+00:00",
                        )
                    )

                    latest_timestamp = (
                        dt.timestamp()
                    )

                except Exception:
                    pass

    if latest is None:
        raise RuntimeError(
            "最新 Codex session 中未找到 rate_limits"
        )

    # 如果 JSONL 没拿到 timestamp，
    # 使用 session 文件修改时间兜底。
    if latest_timestamp is None:
        latest_timestamp = (
            session_file.stat().st_mtime
        )

    return (
        latest,
        session_file,
        latest_timestamp,
    )


# ============================================================
# 快照年龄
# ============================================================

def get_snapshot_age():
    try:
        (
            _,
            _,
            snapshot_timestamp,
        ) = get_latest_rate_limits()

    except Exception:
        return None

    return max(
        0,
        time.time() - snapshot_timestamp,
    )


# ============================================================
# Codex 主动刷新
# ============================================================

def refresh_codex_quota():
    """
    通过一次最小 Codex 请求刷新本地 rate_limits。

    注意：
    - 不允许修改文件
    - 使用 read-only sandbox
    - 不执行 shell 操作
    - 仅用于产生新的额度快照
    """

    command = [
        CODEX_BIN,
        "exec",
        "--sandbox",
        "read-only",
        "--skip-git-repo-check",
        "Reply with exactly: OK",
    ]

    result = subprocess.run(
        command,
        cwd="/ops/apps/tg-codex-agent",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=CODEX_REFRESH_TIMEOUT,
        env={
            **os.environ,
            "CODEX_HOME": str(CODEX_HOME),
        },
    )

    if result.returncode != 0:
        error_text = (
            result.stderr.strip()
            or result.stdout.strip()
            or "unknown error"
        )

        raise RuntimeError(
            f"Codex 刷新失败：{error_text[:300]}"
        )

    # Codex 请求完成后给文件系统一点时间
    time.sleep(1)


# ============================================================
# 获取额度
# ============================================================

def get_rate_limits(
    refresh_if_stale=False,
):
    """
    refresh_if_stale=True：

    当额度快照超过 QUOTA_SNAPSHOT_MAX_AGE，
    自动执行一次 Codex 最小请求刷新。
    """

    should_refresh = False

    try:
        age = get_snapshot_age()

        if age is None:
            should_refresh = True

        elif age > QUOTA_SNAPSHOT_MAX_AGE:
            should_refresh = True

    except Exception:
        should_refresh = True

    if (
        refresh_if_stale
        and should_refresh
    ):
        refresh_codex_quota()

    return get_latest_rate_limits()


# ============================================================
# 格式化
# ============================================================

def format_reset(timestamp):
    if not timestamp:
        return "未知"

    dt = datetime.fromtimestamp(
        timestamp,
        tz=LOCAL_TZ,
    )

    return dt.strftime(
        "%m-%d %H:%M"
    )


def format_snapshot_time(timestamp):
    if not timestamp:
        return "未知"

    dt = datetime.fromtimestamp(
        timestamp,
        tz=LOCAL_TZ,
    )

    return dt.strftime(
        "%m-%d %H:%M:%S"
    )


# ============================================================
# TG 输出
# ============================================================

def build_usage_text(
    threshold=10,
    refresh_if_stale=True,
):
    (
        rate_limits,
        session_file,
        snapshot_timestamp,
    ) = get_rate_limits(
        refresh_if_stale=refresh_if_stale
    )

    primary = (
        rate_limits.get("primary")
        or {}
    )

    secondary = (
        rate_limits.get("secondary")
        or {}
    )

    primary_used = float(
        primary.get(
            "used_percent",
            0,
        )
    )

    secondary_used = float(
        secondary.get(
            "used_percent",
            0,
        )
    )

    primary_left = max(
        0,
        100 - primary_used,
    )

    secondary_left = max(
        0,
        100 - secondary_used,
    )

    plan = (
        rate_limits.get("plan_type")
        or "unknown"
    )

    snapshot_age = max(
        0,
        int(
            time.time()
            - snapshot_timestamp
        ),
    )

    if snapshot_age < 60:
        freshness = "刚刚更新"

    elif snapshot_age < 3600:
        freshness = (
            f"{snapshot_age // 60} 分钟前"
        )

    else:
        freshness = (
            f"{snapshot_age // 3600} 小时前"
        )

    text = (
        "🤖 Codex 额度状态\n\n"

        f"套餐：{plan.capitalize()}\n\n"

        "⏱ 5小时额度\n"
        f"已使用：{primary_used:g}%\n"
        f"剩余：{primary_left:g}%\n"
        f"重置："
        f"{format_reset(primary.get('resets_at'))}"
        "\n\n"

        "📅 周额度\n"
        f"已使用：{secondary_used:g}%\n"
        f"剩余：{secondary_left:g}%\n"
        f"重置："
        f"{format_reset(secondary.get('resets_at'))}"
        "\n\n"

        f"⚠️ 告警阈值：{threshold}%\n"
        f"🕒 数据状态：{freshness}"
    )

    return text


# ============================================================
# CLI 测试
# ============================================================

if __name__ == "__main__":
    print(
        build_usage_text(
            refresh_if_stale=True
        )
    )
