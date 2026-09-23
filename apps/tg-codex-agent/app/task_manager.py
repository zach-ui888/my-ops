import asyncio
import json
import uuid
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


# ============================================================
# 配置
# ============================================================

TASK_DIR = Path(
    "/ops/apps/tg-codex-agent/data/tasks"
)

TIMEZONE = ZoneInfo(
    "Asia/Shanghai"
)

# V1：整个 Agent 同一时间只允许执行一个 Codex Task
TASK_LOCK = asyncio.Lock()


# ============================================================
# 基础工具
# ============================================================

def now_text():
    return datetime.now(
        TIMEZONE
    ).isoformat(
        timespec="seconds"
    )


def generate_task_id():
    timestamp = datetime.now(
        TIMEZONE
    ).strftime(
        "%Y%m%d-%H%M%S"
    )

    random_part = (
        uuid.uuid4()
        .hex[:6]
        .upper()
    )

    return (
        f"{timestamp}-{random_part}"
    )


def task_file(task_id):
    return TASK_DIR / (
        f"{task_id}.json"
    )


# ============================================================
# JSON 原子写入
# ============================================================

def save_task(task):
    TASK_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    path = task_file(
        task["task_id"]
    )

    temp_path = path.with_suffix(
        ".json.tmp"
    )

    with temp_path.open(
        "w",
        encoding="utf-8",
    ) as f:
        json.dump(
            task,
            f,
            ensure_ascii=False,
            indent=2,
        )

    temp_path.replace(
        path
    )


# ============================================================
# 创建任务
# ============================================================

def create_task(
    user_id,
    project,
    prompt,
):
    task_id = generate_task_id()

    task = {
        "task_id": task_id,

        "user_id": int(
            user_id
        ),

        "project": project,

        "prompt": prompt,

        "status": "pending",

        "created_at": now_text(),

        "started_at": None,

        "finished_at": None,

        "result": None,

        "error": None,
    }

    save_task(
        task
    )

    return task


# ============================================================
# 读取任务
# ============================================================

def load_task(task_id):
    path = task_file(
        task_id
    )

    if not path.exists():
        return None

    with path.open(
        "r",
        encoding="utf-8",
    ) as f:
        return json.load(
            f
        )


# ============================================================
# 更新状态
# ============================================================

def mark_running(task):
    task["status"] = "running"
    task["started_at"] = now_text()

    save_task(
        task
    )


def mark_success(
    task,
    result,
):
    task["status"] = "success"
    task["finished_at"] = now_text()
    task["result"] = result
    task["error"] = None

    save_task(
        task
    )


def mark_failed(
    task,
    error,
):
    task["status"] = "failed"
    task["finished_at"] = now_text()
    task["error"] = str(
        error
    )

    save_task(
        task
    )


# ============================================================
# 状态显示
# ============================================================

def status_text(status):
    mapping = {
        "pending": "等待执行",
        "running": "执行中",
        "success": "执行成功",
        "failed": "执行失败",
    }

    return mapping.get(
        status,
        status,
    )


# ============================================================
# CLI 自检
# ============================================================

if __name__ == "__main__":
    test_task = create_task(
        user_id=8526183086,
        project="codex-test",
        prompt="TASK MANAGER TEST",
    )

    print(
        "TASK CREATED"
    )

    print(
        json.dumps(
            test_task,
            ensure_ascii=False,
            indent=2,
        )
    )

    loaded = load_task(
        test_task["task_id"]
    )

    if loaded is None:
        raise RuntimeError(
            "任务读取失败"
        )

    if (
        loaded["task_id"]
        != test_task["task_id"]
    ):
        raise RuntimeError(
            "Task ID 校验失败"
        )

    print()
    print(
        "TASK LOAD OK"
    )
