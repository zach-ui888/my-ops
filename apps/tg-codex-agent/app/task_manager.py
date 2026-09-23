import asyncio
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import uuid
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


BASE_DIR = Path("/ops/apps/tg-codex-agent")
TASK_DIR = BASE_DIR / "data" / "tasks"
SNAPSHOT_DIR = BASE_DIR / "data" / "snapshots"

TIMEZONE = ZoneInfo(os.getenv("TZ", "Asia/Shanghai"))

TASK_LOCK = asyncio.Lock()

SNAPSHOT_IGNORE = {
    ".git",
    "__pycache__",
    ".DS_Store",
    "venv",
    ".venv",
}

TASK_ID_PATTERN = re.compile(
    r"^[0-9]{8}-[0-9]{6}-[A-F0-9]{6}$"
)


def validate_task_id(task_id):
    if not isinstance(task_id, str):
        raise ValueError("Invalid Task ID")

    if not TASK_ID_PATTERN.fullmatch(task_id):
        raise ValueError(
            f"Invalid Task ID: {task_id}"
        )

    return task_id


def now_text():
    return datetime.now(TIMEZONE).isoformat(timespec="seconds")


def generate_task_id():
    timestamp = datetime.now(TIMEZONE).strftime("%Y%m%d-%H%M%S")
    suffix = uuid.uuid4().hex[:6].upper()
    return f"{timestamp}-{suffix}"


def task_file(task_id):
    task_id = validate_task_id(task_id)
    return TASK_DIR / f"{task_id}.json"


def snapshot_root(task_id):
    task_id = validate_task_id(task_id)
    return SNAPSHOT_DIR / task_id


def snapshot_path(task_id, stage):
    if stage not in {"before", "after"}:
        raise ValueError(f"Invalid snapshot stage: {stage}")

    return snapshot_root(task_id) / stage


def ensure_dirs():
    TASK_DIR.mkdir(parents=True, exist_ok=True)
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)

    os.chmod(TASK_DIR, 0o700)
    os.chmod(SNAPSHOT_DIR, 0o700)


def save_task(task):
    ensure_dirs()

    path = task_file(task["task_id"])
    tmp_path = path.with_suffix(".json.tmp")

    with tmp_path.open("w", encoding="utf-8") as f:
        json.dump(
            task,
            f,
            ensure_ascii=False,
            indent=2,
        )
        f.write("\n")

    os.chmod(tmp_path, 0o600)
    tmp_path.replace(path)
    os.chmod(path, 0o600)


def create_task(user_id, project, prompt):
    task_id = generate_task_id()

    task = {
        "task_id": task_id,
        "user_id": int(user_id),
        "project": project,
        "prompt": prompt,
        "status": "pending",
        "created_at": now_text(),
        "started_at": None,
        "finished_at": None,
        "result": None,
        "error": None,
        "snapshot_before": False,
        "snapshot_after": False,
        "approval_status": "pending",
        "approved_at": None,
        "rejected_at": None,
        "git_commit": None,
    }

    save_task(task)
    return task


def load_task(task_id):
    path = task_file(task_id)

    if not path.exists():
        return None

    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def mark_running(task):
    task["status"] = "running"
    task["started_at"] = now_text()
    save_task(task)


def mark_success(task, result):
    task["status"] = "success"
    task["finished_at"] = now_text()
    task["result"] = result
    task["error"] = None
    save_task(task)


def mark_failed(task, error):
    task["status"] = "failed"
    task["finished_at"] = now_text()
    task["error"] = str(error)
    save_task(task)


def mark_approved(task, git_commit=None):
    task["approval_status"] = "approved"
    task["approved_at"] = now_text()

    if git_commit:
        task["git_commit"] = git_commit

    save_task(task)


def mark_rejected(task):
    task["approval_status"] = "rejected"
    task["rejected_at"] = now_text()
    save_task(task)


def copy_project_tree(source, destination):
    source = Path(source).resolve()
    destination = Path(destination).resolve()

    if not source.exists():
        raise FileNotFoundError(f"Project does not exist: {source}")

    if not source.is_dir():
        raise NotADirectoryError(f"Project is not a directory: {source}")

    if destination.exists():
        shutil.rmtree(destination)

    destination.parent.mkdir(parents=True, exist_ok=True)

    def ignore(directory, names):
        return [
            name
            for name in names
            if name in SNAPSHOT_IGNORE
        ]

    shutil.copytree(
        source,
        destination,
        symlinks=True,
        ignore=ignore,
    )


def secure_snapshot_permissions(path):
    """
    只保护快照入口目录。

    快照内部文件和目录保留原项目 mode，
    这样后续 reject / approve 可以正确保留
    executable bit 等权限信息。

    外层 snapshots、task root、before/after
    均由 root-only 目录阻止 codex-runner 访问。
    """
    path = Path(path)

    if not path.exists():
        return

    if path.is_symlink():
        raise ValueError(
            f"Snapshot root cannot be symlink: {path}"
        )

    os.chmod(path, 0o700)


def create_snapshot(task_id, project_path, stage):
    ensure_dirs()

    root = snapshot_root(task_id)
    root.mkdir(parents=True, exist_ok=True)
    os.chmod(root, 0o700)

    destination = snapshot_path(task_id, stage)

    copy_project_tree(
        project_path,
        destination,
    )

    secure_snapshot_permissions(destination)

    task = load_task(task_id)

    if task:
        task[f"snapshot_{stage}"] = True
        save_task(task)

    return destination


def reconcile_snapshot_state(task_id):
    """
    以磁盘上的实际快照为准，修复旧 Task JSON 中可能错误的
    snapshot_before / snapshot_after 状态。

    返回：
        (task, before_exists, after_exists)
    """
    task = load_task(task_id)

    if not task:
        return None, False, False

    before = snapshot_path(
        task_id,
        "before",
    )

    after = snapshot_path(
        task_id,
        "after",
    )

    before_exists = (
        before.exists()
        and before.is_dir()
    )

    after_exists = (
        after.exists()
        and after.is_dir()
    )

    changed = False

    if task.get("snapshot_before") != before_exists:
        task["snapshot_before"] = before_exists
        changed = True

    if task.get("snapshot_after") != after_exists:
        task["snapshot_after"] = after_exists
        changed = True

    if changed:
        save_task(task)

    return (
        task,
        before_exists,
        after_exists,
    )


def sha256_file(path):
    digest = hashlib.sha256()

    with Path(path).open("rb") as f:
        while True:
            chunk = f.read(1024 * 1024)

            if not chunk:
                break

            digest.update(chunk)

    return digest.hexdigest()


def scan_snapshot_tree(root):
    """
    扫描快照树，不跟随 symlink。

    V1 reject / approve 只接受：
    - 普通文件
    - 普通目录

    symlink、socket、FIFO、device 等特殊类型直接拒绝。
    """
    root = Path(root)

    if not root.exists():
        raise FileNotFoundError(
            f"Snapshot does not exist: {root}"
        )

    if not root.is_dir():
        raise NotADirectoryError(
            f"Snapshot is not a directory: {root}"
        )

    entries = {}

    for current_root, dirs, files in os.walk(
        root,
        topdown=True,
        followlinks=False,
    ):
        current_root = Path(current_root)

        # 先检查目录，防止 os.walk 继续进入 symlink。
        for dirname in list(dirs):
            path = current_root / dirname
            info = path.lstat()
            relative = path.relative_to(root).as_posix()

            if stat.S_ISLNK(info.st_mode):
                raise ValueError(
                    f"Symlink is not allowed in task snapshot: {relative}"
                )

            if not stat.S_ISDIR(info.st_mode):
                raise ValueError(
                    f"Special path is not allowed in task snapshot: {relative}"
                )

            entries[relative] = {
                "type": "directory",
                "mode": stat.S_IMODE(info.st_mode),
                "sha256": None,
            }

        for filename in files:
            path = current_root / filename
            info = path.lstat()
            relative = path.relative_to(root).as_posix()

            if stat.S_ISLNK(info.st_mode):
                raise ValueError(
                    f"Symlink is not allowed in task snapshot: {relative}"
                )

            if not stat.S_ISREG(info.st_mode):
                raise ValueError(
                    f"Special file is not allowed in task snapshot: {relative}"
                )

            entries[relative] = {
                "type": "file",
                "mode": stat.S_IMODE(info.st_mode),
                "sha256": sha256_file(path),
            }

    return entries


def build_change_manifest(task_id):
    """
    比较 Task before / after 快照。

    该函数只读，不修改 Live 项目。
    """
    task_id = validate_task_id(task_id)

    before_root = snapshot_path(
        task_id,
        "before",
    )

    after_root = snapshot_path(
        task_id,
        "after",
    )

    before = scan_snapshot_tree(before_root)
    after = scan_snapshot_tree(after_root)

    changes = []

    all_paths = sorted(
        set(before) | set(after)
    )

    for relative in all_paths:
        before_entry = before.get(relative)
        after_entry = after.get(relative)

        if before_entry is None:
            change_type = "added"

        elif after_entry is None:
            change_type = "deleted"

        elif before_entry != after_entry:
            change_type = "modified"

        else:
            continue

        changes.append(
            {
                "path": relative,
                "change": change_type,
                "before": before_entry,
                "after": after_entry,
            }
        )

    return changes


def inspect_live_path(path):
    """
    返回 Live 路径当前状态。

    V1 只接受普通文件和普通目录。
    symlink / FIFO / socket / device 等直接标记 unsupported。
    """
    path = Path(path)

    try:
        info = path.lstat()
    except FileNotFoundError:
        return None

    mode = stat.S_IMODE(info.st_mode)

    if stat.S_ISLNK(info.st_mode):
        return {
            "type": "unsupported",
            "subtype": "symlink",
            "mode": mode,
            "sha256": None,
        }

    if stat.S_ISREG(info.st_mode):
        return {
            "type": "file",
            "mode": mode,
            "sha256": sha256_file(path),
        }

    if stat.S_ISDIR(info.st_mode):
        return {
            "type": "directory",
            "mode": mode,
            "sha256": None,
        }

    return {
        "type": "unsupported",
        "subtype": "special",
        "mode": mode,
        "sha256": None,
    }


def check_reject_conflicts(task_id, project_path):
    """
    检查当前 Live 项目是否仍然等于 Task 的 After 状态。

    只有没有冲突时，后续才允许真正 reject。

    当前阶段只检查，不修改任何 Live 文件。
    """
    task_id = validate_task_id(task_id)

    project_root = Path(project_path).resolve()

    if not project_root.exists():
        raise FileNotFoundError(
            f"Project does not exist: {project_root}"
        )

    if not project_root.is_dir():
        raise NotADirectoryError(
            f"Project is not a directory: {project_root}"
        )

    changes = build_change_manifest(task_id)
    conflicts = []

    for item in changes:
        relative = item["path"]
        expected_after = item["after"]

        live_path = (
            project_root / relative
        )

        live_state = inspect_live_path(
            live_path
        )

        # After 中不存在：
        # Task 当时删除了这个路径。
        # Live 也必须继续不存在。
        if expected_after is None:
            if live_state is not None:
                conflicts.append(
                    {
                        "path": relative,
                        "reason": (
                            "path_exists_but_task_after_deleted_it"
                        ),
                        "live": live_state,
                        "expected_after": None,
                    }
                )

            continue

        # After 中存在，但 Live 已不存在。
        if live_state is None:
            conflicts.append(
                {
                    "path": relative,
                    "reason": "live_path_missing",
                    "live": None,
                    "expected_after": expected_after,
                }
            )
            continue

        # 特殊文件一律拒绝。
        if live_state.get("type") == "unsupported":
            conflicts.append(
                {
                    "path": relative,
                    "reason": "unsupported_live_path",
                    "live": live_state,
                    "expected_after": expected_after,
                }
            )
            continue

        # 类型必须一致。
        if live_state.get("type") != expected_after.get("type"):
            conflicts.append(
                {
                    "path": relative,
                    "reason": "path_type_changed",
                    "live": live_state,
                    "expected_after": expected_after,
                }
            )
            continue

        # 普通文件以 SHA-256 为主要冲突判断。
        #
        # 旧 Task 的快照曾被历史逻辑强制 chmod 0600，
        # 因此暂时不把 mode 差异作为冲突条件。
        if expected_after.get("type") == "file":
            if (
                live_state.get("sha256")
                != expected_after.get("sha256")
            ):
                conflicts.append(
                    {
                        "path": relative,
                        "reason": "file_content_changed",
                        "live": live_state,
                        "expected_after": expected_after,
                    }
                )

    return {
        "safe": not conflicts,
        "change_count": len(changes),
        "conflicts": conflicts,
    }


def reject_task_changes(task_id, project_path):
    """
    安全撤销一个 Task 对 Live 项目的修改。

    前提：
    1. Task before / after 快照完整。
    2. 当前 Live 中所有相关路径仍等于 Task 的 after 状态。
    3. changed path 只能是普通文件或普通目录。
    4. 发现冲突时，一个文件都不修改。

    返回回滚结果。
    """
    task_id = validate_task_id(task_id)

    task = load_task(task_id)

    if not task:
        raise FileNotFoundError(
            f"Task not found: {task_id}"
        )

    if task.get("status") != "success":
        raise ValueError(
            f"Task is not successful: {task_id}"
        )

    if task.get("approval_status") != "pending":
        raise ValueError(
            "Task approval status is not pending: "
            f"{task.get('approval_status')}"
        )

    project_root = Path(project_path).resolve()

    if not project_root.exists():
        raise FileNotFoundError(
            f"Project does not exist: {project_root}"
        )

    if not project_root.is_dir():
        raise NotADirectoryError(
            f"Project is not a directory: {project_root}"
        )

    conflict_result = check_reject_conflicts(
        task_id,
        project_root,
    )

    if not conflict_result["safe"]:
        raise RuntimeError(
            "Reject conflict detected: "
            + json.dumps(
                conflict_result["conflicts"],
                ensure_ascii=False,
            )
        )

    before_root = snapshot_path(
        task_id,
        "before",
    )

    changes = build_change_manifest(task_id)

    # 先处理文件，再处理目录。
    #
    # added:
    #   before 不存在 -> 删除 Live
    #
    # modified/deleted:
    #   before 存在 -> 从 before 恢复
    file_changes = [
        item
        for item in changes
        if (
            (item["before"] or item["after"])
            .get("type")
            == "file"
        )
    ]

    directory_changes = [
        item
        for item in changes
        if (
            (item["before"] or item["after"])
            .get("type")
            == "directory"
        )
    ]

    for item in file_changes:
        relative = item["path"]
        before_entry = item["before"]
        live_path = project_root / relative

        if before_entry is None:
            # Task 新增的文件：reject 时删除。
            if live_path.exists():
                live_path.unlink()

            continue

        source = before_root / relative

        if not source.exists():
            raise FileNotFoundError(
                f"Before snapshot file missing: {relative}"
            )

        live_path.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        shutil.copy2(
            source,
            live_path,
            follow_symlinks=False,
        )

    # 目录按深度倒序处理删除，再正序恢复。
    added_directories = [
        item
        for item in directory_changes
        if item["before"] is None
    ]

    for item in sorted(
        added_directories,
        key=lambda x: x["path"].count("/"),
        reverse=True,
    ):
        live_path = project_root / item["path"]

        if live_path.exists():
            try:
                live_path.rmdir()
            except OSError:
                # 非空目录不强删，避免误删后续内容。
                raise RuntimeError(
                    "Reject refused to remove non-empty "
                    f"directory: {item['path']}"
                )

    restore_directories = [
        item
        for item in directory_changes
        if item["before"] is not None
    ]

    for item in sorted(
        restore_directories,
        key=lambda x: x["path"].count("/"),
    ):
        relative = item["path"]
        source = before_root / relative
        live_path = project_root / relative

        if not source.exists():
            raise FileNotFoundError(
                f"Before snapshot directory missing: {relative}"
            )

        live_path.mkdir(
            parents=True,
            exist_ok=True,
        )

        os.chmod(
            live_path,
            item["before"]["mode"],
        )

    task = load_task(task_id)
    mark_rejected(task)

    return {
        "task_id": task_id,
        "project": task.get("project"),
        "change_count": len(changes),
        "status": "rejected",
    }


def build_task_diff(task_id):
    before = snapshot_path(task_id, "before")
    after = snapshot_path(task_id, "after")

    if not before.exists():
        raise FileNotFoundError(
            f"Before snapshot not found for task: {task_id}"
        )

    if not after.exists():
        raise FileNotFoundError(
            f"After snapshot not found for task: {task_id}"
        )

    command = [
        "diff",
        "-ruN",
        "--exclude=__pycache__",
        "--exclude=.git",
        str(before),
        str(after),
    ]

    result = subprocess.run(
        command,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=60,
        check=False,
    )

    # diff:
    # 0 = no differences
    # 1 = differences found
    # >1 = error
    if result.returncode not in {0, 1}:
        error = result.stderr.strip() or "Unknown diff error"
        raise RuntimeError(error)

    return result.stdout


def remove_snapshots(task_id):
    path = snapshot_root(task_id)

    if path.exists():
        shutil.rmtree(path)


def status_text(task):
    if not task:
        return "not_found"

    return task.get("status", "unknown")


if __name__ == "__main__":
    ensure_dirs()

    test_task = create_task(
        user_id=0,
        project="snapshot-self-test",
        prompt="task manager self test",
    )

    print(json.dumps(test_task, ensure_ascii=False, indent=2))
