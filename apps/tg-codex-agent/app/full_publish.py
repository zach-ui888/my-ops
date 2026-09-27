import hashlib
import stat
import subprocess
from pathlib import Path

from task_manager import (
    build_initial_publish_changes,
    is_approval_path_blocked,
    load_task,
    scan_snapshot_tree,
    snapshot_path,
    validate_approval_project,
    validate_approval_relative_path,
    validate_task_id,
)


def scan_git_tracked_tree(project, repo_root="/root/my-ops"):
    """
    读取 Git HEAD 中指定项目的 tracked tree。

    这里只接受：
    - regular file

    明确拒绝：
    - symlink
    - submodule
    - 其它未知 Git mode

    返回格式与 scan_snapshot_tree() 的 file entry 兼容。
    """
    project = validate_approval_project(project)
    repo_root = Path(repo_root).resolve()

    prefix = f"apps/{project}/"

    result = subprocess.run(
        [
            "git",
            "-C",
            str(repo_root),
            "ls-files",
            "-s",
            "-z",
            "--",
            f"apps/{project}/**",
        ],
        capture_output=True,
        check=True,
    )

    entries = {}

    for raw in result.stdout.split(b"\0"):
        if not raw:
            continue

        try:
            metadata, raw_path = raw.split(b"\t", 1)
            mode_raw, _object_id, stage_raw = metadata.split(
                b" ",
                2,
            )
            git_path = raw_path.decode(
                "utf-8",
                errors="strict",
            )
        except (ValueError, UnicodeDecodeError) as exc:
            raise RuntimeError(
                "Unable to parse git ls-files output"
            ) from exc

        if stage_raw != b"0":
            raise RuntimeError(
                f"Git index contains non-stage-0 entry: {git_path}"
            )

        if not git_path.startswith(prefix):
            raise RuntimeError(
                f"Git path escaped project scope: {git_path}"
            )

        relative = git_path[len(prefix):]
        validate_approval_relative_path(relative)

        mode_text = mode_raw.decode("ascii")

        if mode_text == "120000":
            raise RuntimeError(
                f"Tracked symlink is not allowed: {relative}"
            )

        if mode_text == "160000":
            raise RuntimeError(
                f"Tracked submodule is not allowed: {relative}"
            )

        if mode_text not in {"100644", "100755"}:
            raise RuntimeError(
                "Unsupported tracked Git mode "
                f"{mode_text}: {relative}"
            )

        blob = subprocess.run(
            [
                "git",
                "-C",
                str(repo_root),
                "show",
                f"HEAD:{git_path}",
            ],
            capture_output=True,
            check=True,
        ).stdout

        entries[relative] = {
            "type": "file",
            "mode": (
                0o755
                if mode_text == "100755"
                else 0o644
            ),
            "sha256": hashlib.sha256(blob).hexdigest(),
        }

    return entries


def build_full_publish_plan(
    task_id,
    repo_root="/root/my-ops",
):
    """
    构建已有 Git 项目的完整发布计划。

    目标状态：
        当前 Git tracked project
            ->
        immutable Task After Snapshot 中所有允许发布内容

    blocked 路径（secrets/runtime/reference/...）永不进入计划。

    本函数只读：
    - 不修改 Live
    - 不修改 Git working tree/index
    - 不 commit
    - 不 push
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

    if task.get("approval_status", "pending") != "pending":
        raise ValueError(
            "Task approval status is not pending: "
            f"{task.get('approval_status')}"
        )

    project = validate_approval_project(
        task.get("project")
    )

    after_root = snapshot_path(
        task_id,
        "after",
    )

    if not after_root.is_dir():
        raise FileNotFoundError(
            "After snapshot does not exist"
        )

    repo_root = Path(repo_root).resolve()
    repo_project = repo_root / "apps" / project

    if not repo_project.exists():
        raise RuntimeError(
            "Full publish requires an existing Git project; "
            "use normal /approve for initial publish"
        )

    # Snapshot 本身可能包含 runtime 等运行态目录。
    # Full publish 的目标树必须先执行 Approval block policy。
    raw_after = scan_snapshot_tree(after_root)

    after = {
        relative: entry
        for relative, entry in raw_after.items()
        if (
            entry.get("type") == "file"
            and not is_approval_path_blocked(relative)
        )
    }

    before = scan_git_tracked_tree(
        project,
        repo_root=repo_root,
    )

    # Git 不跟踪目录本身。
    # 为复用现有 staging/live-check，我们把 After 中允许的目录
    # 保留；before 仅包含 Git tracked files。
    all_paths = sorted(
        set(before) | set(after)
    )

    changes = []

    for relative in all_paths:
        validate_approval_relative_path(relative)

        before_entry = before.get(relative)
        after_entry = after.get(relative)

        # After directory 只是用于确保目录存在。
        # 如果 Git before 没有该 entry，它可以作为 ADD directory。
        if before_entry is None:
            change = "added"

        elif after_entry is None:
            change = "deleted"

        elif before_entry.get("type") != after_entry.get("type"):
            raise RuntimeError(
                "Full publish does not allow path type changes: "
                f"{relative}"
            )

        elif before_entry.get("type") == "file":
            before_executable = bool(
                before_entry.get("mode", 0) & 0o111
            )
            after_executable = bool(
                after_entry.get("mode", 0) & 0o111
            )

            if (
                before_entry.get("sha256")
                == after_entry.get("sha256")
                and before_executable == after_executable
            ):
                continue

            change = "modified"

        else:
            continue

        changes.append(
            {
                "path": relative,
                "change": change,
                "before": before_entry,
                "after": after_entry,
            }
        )

    plan = []

    for item in changes:
        if item["change"] == "added":
            action = "ADD"
        elif item["change"] == "modified":
            action = "MODIFY"
        elif item["change"] == "deleted":
            action = "DELETE"
        else:
            raise RuntimeError(
                "Unknown full publish change type"
            )

        plan.append(
            {
                "action": action,
                "path": item["path"],
                "before": item["before"],
                "after": item["after"],
            }
        )

    return {
        "task_id": task_id,
        "project": project,
        "status": task.get("status"),
        "approval_status": task.get(
            "approval_status",
            "pending",
        ),
        "change_count": len(plan),
        "initial_publish": False,
        "full_publish": True,
        "changes": plan,
    }


def publish_full_task_to_git(
    task_id,
    repo_root="/root/my-ops",
):
    """
    将已有 Git 项目完整同步到指定 Task 的 immutable After Snapshot。

    Full Publish 只负责构建完整计划。
    实际 staging / secret scan / live check / commit / push /
    origin SHA verification / approval state 全部复用 task_manager
    的正式发布链。
    """
    from task_manager import publish_task_to_git

    plan = build_full_publish_plan(
        task_id,
        repo_root=repo_root,
    )

    if not plan.get("full_publish"):
        raise RuntimeError(
            "Refusing publish: plan is not a Full Publish plan"
        )

    if plan.get("initial_publish"):
        raise RuntimeError(
            "Refusing publish: Full Publish cannot be initial publish"
        )

    if not plan.get("changes"):
        raise RuntimeError(
            "Full Publish produced no Git changes"
        )

    result = publish_task_to_git(
        task_id,
        repo_root=repo_root,
        plan=plan,
    )

    result["full_publish"] = True
    result["full_change_count"] = plan["change_count"]

    return result
