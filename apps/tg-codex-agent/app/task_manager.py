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
    "secrets",
    ".test-work",
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


def mark_publish_state(
    task,
    state,
    git_commit=None,
    error=None,
):
    """
    记录 Git 发布过程，但不改变 approval_status。

    state 示例：
    - staging
    - committed
    - push_failed
    - pushed
    """
    task["publish_state"] = state
    task["publish_updated_at"] = now_text()

    if git_commit:
        task["git_commit"] = git_commit

    if error:
        task["publish_error"] = str(error)[:1000]
    else:
        task["publish_error"] = None

    save_task(task)


def clear_publish_state(task):
    """
    清理未完成发布状态。
    approval_status 不受影响。
    """
    task["publish_state"] = None
    task["publish_updated_at"] = now_text()
    task["publish_error"] = None

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


APPROVAL_PROJECT_PATTERN = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]*$"
)

APPROVAL_PROTECTED_PROJECTS = {
    "tg-codex-agent",
}

APPROVAL_BLOCKED_PARTS = {
    ".git",
    ".codex",
    ".env",
    "data",
    "logs",
    "venv",
    ".venv",
    "backup",
    "backups",
    "secrets",
    ".test-work",
    "reference",
}

APPROVAL_BLOCKED_FILENAMES = {
    "id_rsa",
    "id_ed25519",
    "authorized_keys",
}

APPROVAL_BLOCKED_SUFFIXES = {
    ".pem",
    ".key",
    ".p12",
    ".pfx",
}


def validate_approval_project(project):
    if not isinstance(project, str):
        raise ValueError("Invalid project name")

    if not APPROVAL_PROJECT_PATTERN.fullmatch(project):
        raise ValueError(
            f"Invalid project name: {project}"
        )

    if project in {".", ".."}:
        raise ValueError(
            f"Invalid project name: {project}"
        )

    if project in APPROVAL_PROTECTED_PROJECTS:
        raise ValueError(
            f"Protected project cannot be approved: {project}"
        )

    return project


def validate_approval_relative_path(relative):
    """
    Approval V1 路径策略。

    只检查 manifest 中的相对路径，不访问 Git 仓库。
    """
    if not isinstance(relative, str) or not relative:
        raise ValueError("Invalid approval path")

    pure = Path(relative)

    if pure.is_absolute():
        raise ValueError(
            f"Absolute path is not allowed: {relative}"
        )

    parts = pure.parts

    if not parts:
        raise ValueError(
            f"Invalid approval path: {relative}"
        )

    for part in parts:
        if part in {"", ".", ".."}:
            raise ValueError(
                f"Unsafe approval path: {relative}"
            )

        if part.lower() in APPROVAL_BLOCKED_PARTS:
            raise ValueError(
                f"Blocked approval path: {relative}"
            )

    filename = parts[-1]
    filename_lower = filename.lower()

    if filename_lower in APPROVAL_BLOCKED_FILENAMES:
        raise ValueError(
            f"Blocked credential path: {relative}"
        )

    if (
        filename_lower.startswith(".env")
        and filename_lower != ".env.example"
    ):
        raise ValueError(
            f"Blocked environment file: {relative}"
        )

    if any(
        filename_lower.endswith(suffix)
        for suffix in APPROVAL_BLOCKED_SUFFIXES
    ):
        raise ValueError(
            f"Blocked credential file: {relative}"
        )

    return relative


def is_approval_path_blocked(relative):
    """
    判断路径是否属于 Approval 固定禁止发布范围。

    本函数只用于首次完整项目发布时过滤 After Snapshot。
    普通增量 Approval 仍由 validate_approval_relative_path()
    严格拒绝非法路径。
    """
    try:
        validate_approval_relative_path(relative)
        return False
    except ValueError:
        return True


def build_initial_publish_changes(task_id):
    """
    为 Git 中尚不存在的新项目构建首次完整发布计划。

    数据来源只能是 immutable After Snapshot。

    固定禁止路径不会进入发布候选，例如：
    - secrets/
    - reference/
    - .test-work/
    - data/
    - logs/
    - venv/
    - .env

    .env.example 允许发布。
    """
    task_id = validate_task_id(task_id)

    after_root = snapshot_path(
        task_id,
        "after",
    )

    after = scan_snapshot_tree(after_root)

    changes = []

    for relative in sorted(after):
        if is_approval_path_blocked(relative):
            continue

        after_entry = after[relative]

        changes.append(
            {
                "path": relative,
                "change": "added",
                "before": None,
                "after": after_entry,
            }
        )

    return changes


def build_approval_plan(task_id, repo_root="/root/my-ops"):
    """
    构建 Approval 只读计划。

    本函数：
    - 不修改 Live 项目
    - 不修改 /root/my-ops
    - 不执行 git add / commit / push
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

    before_root = snapshot_path(
        task_id,
        "before",
    )

    after_root = snapshot_path(
        task_id,
        "after",
    )

    if not before_root.is_dir():
        raise FileNotFoundError(
            "Before snapshot does not exist"
        )

    if not after_root.is_dir():
        raise FileNotFoundError(
            "After snapshot does not exist"
        )

    repo_root = Path(repo_root).resolve()

    repo_project = (
        repo_root
        / "apps"
        / project
    )

    initial_publish = not repo_project.exists()

    if initial_publish:
        changes = build_initial_publish_changes(task_id)
    else:
        changes = build_change_manifest(task_id)

    plan = []

    for item in changes:
        relative = validate_approval_relative_path(
            item["path"]
        )

        before_entry = item["before"]
        after_entry = item["after"]

        # V1 不处理 file <-> directory 类型转换。
        if (
            before_entry is not None
            and after_entry is not None
            and before_entry.get("type")
            != after_entry.get("type")
        ):
            raise ValueError(
                "Approval V1 does not allow path type "
                f"changes: {relative}"
            )

        if item["change"] == "added":
            action = "ADD"

        elif item["change"] == "deleted":
            action = "DELETE"

        elif item["change"] == "modified":
            action = "MODIFY"

        else:
            raise ValueError(
                "Unknown manifest change type: "
                f"{item['change']}"
            )

        plan.append(
            {
                "action": action,
                "path": relative,
                "before": before_entry,
                "after": after_entry,
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
        "initial_publish": initial_publish,
        "changes": plan,
    }


SECRET_CONTENT_PATTERNS = [
    (
        "private_key",
        re.compile(
            rb"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"
        ),
    ),
    (
        "github_token",
        re.compile(
            rb"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}\b"
        ),
    ),
    (
        "github_fine_grained_token",
        re.compile(
            rb"\bgithub_pat_[A-Za-z0-9_]{20,}\b"
        ),
    ),
    (
        "openai_api_key",
        re.compile(
            rb"\bsk-[A-Za-z0-9_-]{20,}\b"
        ),
    ),
    (
        "slack_token",
        re.compile(
            rb"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"
        ),
    ),
    (
        "aws_access_key",
        re.compile(
            rb"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"
        ),
    ),
]


CREDENTIAL_LITERAL_PATTERN = re.compile(
    rb"""(?ix)
    \b
    (?:api[_-]?key|access[_-]?token|auth[_-]?token|
       bot[_-]?token|client[_-]?secret|password|
       passwd|secret)
    \b
    \s*
    [:=]
    \s*
    (?P<quote>["'])
    (?P<value>[^"'\r\n]{16,})
    (?P=quote)
    """
)


CREDENTIAL_TEST_MARKERS = (
    b"fixture",
    b"synthetic",
    b"test-only",
    b"test_only",
    b"not-a-secret",
    b"not_a_secret",
    b"example",
    b"placeholder",
    b"dummy",
    b"fake",
)


def scan_file_for_secrets(path):
    """
    扫描单个普通文件。
    返回规则名称，不返回命中的 secret 内容。
    """
    path = Path(path)

    info = path.lstat()

    if not stat.S_ISREG(info.st_mode):
        raise ValueError(
            f"Secret scan requires regular file: {path}"
        )

    data = path.read_bytes()

    matches = []

    for rule_name, pattern in SECRET_CONTENT_PATTERNS:
        if pattern.search(data):
            matches.append(rule_name)

    # credential_assignment 只针对明确的硬编码字符串字面量。
    #
    # 动态来源，例如 getpass()、环境变量、配置读取、
    # request/JSON 字段和函数参数，不应因为变量名叫
    # password/secret 就被误判。
    #
    # 测试 fixture 使用明确的 synthetic/fixture/fake 等
    # 标记时允许通过；高置信 token/private-key 规则仍由
    # SECRET_CONTENT_PATTERNS 独立阻止。
    for match in CREDENTIAL_LITERAL_PATTERN.finditer(data):
        value = match.group("value")
        value_lower = value.lower()

        if any(
            marker in value_lower
            for marker in CREDENTIAL_TEST_MARKERS
        ):
            continue

        matches.append("credential_assignment")
        break

    return sorted(set(matches))


def scan_approval_snapshot_for_secrets(
    task_id,
    repo_root="/root/my-ops",
):
    """
    只扫描 Task manifest 中 After Snapshot 的新增/修改文件。
    删除文件无需扫描。

    返回：
    {
        "safe": bool,
        "findings": [
            {"path": "...", "rules": [...]}
        ]
    }

    不返回任何 secret 原文。
    """
    task_id = validate_task_id(task_id)

    plan = build_approval_plan(
        task_id,
        repo_root=repo_root,
    )
    after_root = snapshot_path(task_id, "after")

    findings = []

    for item in plan["changes"]:
        after_entry = item["after"]

        if (
            after_entry is None
            or after_entry.get("type") != "file"
        ):
            continue

        relative = item["path"]
        validate_approval_relative_path(relative)

        source = after_root / relative

        rules = scan_file_for_secrets(source)

        if rules:
            findings.append(
                {
                    "path": relative,
                    "rules": rules,
                }
            )

    return {
        "safe": not findings,
        "findings": findings,
    }


def validate_git_publish_state(
    repo_root="/root/my-ops",
    fetch=False,
):
    """
    Git 发布前状态检查。

    要求：
    - 必须是有效 Git 仓库
    - 当前分支必须是 main
    - origin/main 必须存在
    - 本地 HEAD 必须与 origin/main 完全一致

    fetch=True 时先执行 git fetch origin main。
    本函数不 commit、不 push。
    """
    repo_root = Path(repo_root).resolve()

    if not (repo_root / ".git").exists():
        raise ValueError(
            f"Not a Git repository: {repo_root}"
        )

    if fetch:
        subprocess.run(
            [
                "git",
                "-C",
                str(repo_root),
                "fetch",
                "origin",
                "main",
            ],
            check=True,
            capture_output=True,
            text=True,
        )

    branch = subprocess.run(
        [
            "git",
            "-C",
            str(repo_root),
            "branch",
            "--show-current",
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()

    if branch != "main":
        raise RuntimeError(
            f"Git branch must be main, current: {branch}"
        )

    local_head = subprocess.run(
        [
            "git",
            "-C",
            str(repo_root),
            "rev-parse",
            "HEAD",
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()

    remote = subprocess.run(
        [
            "git",
            "-C",
            str(repo_root),
            "rev-parse",
            "--verify",
            "origin/main",
        ],
        capture_output=True,
        text=True,
    )

    if remote.returncode != 0:
        raise RuntimeError(
            "origin/main does not exist"
        )

    origin_head = remote.stdout.strip()

    if local_head != origin_head:
        raise RuntimeError(
            "Local HEAD does not match origin/main"
        )

    return {
        "safe": True,
        "branch": branch,
        "local_head": local_head,
        "origin_head": origin_head,
    }


def validate_staged_scope(
    task_id,
    repo_root="/root/my-ops",
):
    """
    二次验证 Git staging area。

    staging 中出现的每一个路径都必须属于当前 Task manifest。
    不允许夹带任何其它文件。

    注意：
    Task manifest 中的目录不会作为 Git entry 出现，
    因此 expected 只统计 file 类型的 before/after entry。
    """
    task_id = validate_task_id(task_id)

    plan = build_approval_plan(
        task_id,
        repo_root=repo_root,
    )
    project = plan["project"]

    repo_root = Path(repo_root).resolve()

    expected = set()

    for item in plan["changes"]:
        before_entry = item["before"]
        after_entry = item["after"]

        before_is_file = (
            before_entry is not None
            and before_entry.get("type") == "file"
        )

        after_is_file = (
            after_entry is not None
            and after_entry.get("type") == "file"
        )

        if not (before_is_file or after_is_file):
            continue

        relative = validate_approval_relative_path(
            item["path"]
        )

        expected.add(
            (
                Path("apps")
                / project
                / relative
            ).as_posix()
        )

    result = subprocess.run(
        [
            "git",
            "-C",
            str(repo_root),
            "diff",
            "--cached",
            "--name-only",
            "--no-renames",
        ],
        capture_output=True,
        text=True,
        check=True,
    )

    staged = {
        line.strip()
        for line in result.stdout.splitlines()
        if line.strip()
    }

    unexpected = sorted(
        staged - expected
    )

    if unexpected:
        raise RuntimeError(
            "Staged scope violation: unexpected paths: "
            + json.dumps(
                unexpected,
                ensure_ascii=False,
            )
        )

    if not staged:
        raise RuntimeError(
            "Approval produced no staged Git changes"
        )

    return {
        "safe": True,
        "expected": sorted(expected),
        "staged": sorted(staged),
    }


def stage_task_for_approval(
    task_id,
    repo_root="/root/my-ops",
):
    """
    将单个 Task 的变更精确应用到 Git staging area。

    本函数：
    - 要求 Task success + pending
    - 要求 Live 仍等于 Task After Snapshot
    - 要求 Git working tree / index 开始时完全干净
    - 只处理 Task manifest 中的路径
    - 执行 git add --all -- 指定路径
    - 执行 git diff --cached --check
    - 不 commit
    - 不 push
    - 不 mark approved
    """
    task_id = validate_task_id(task_id)

    plan = build_approval_plan(
        task_id,
        repo_root=repo_root,
    )
    project = plan["project"]

    # --------------------------------------------------------
    # Secret Scan 必须发生在任何 Git 工作区修改之前。
    # 只扫描 immutable After Snapshot 中属于本 Task 的文件。
    # 错误信息只包含路径和规则名称，不包含 secret 原文。
    # --------------------------------------------------------
    secret_scan = scan_approval_snapshot_for_secrets(
        task_id,
        repo_root=repo_root,
    )

    if not secret_scan["safe"]:
        safe_findings = [
            {
                "path": item["path"],
                "rules": item["rules"],
            }
            for item in secret_scan["findings"]
        ]

        raise RuntimeError(
            "Secret scan blocked approval: "
            + json.dumps(
                safe_findings,
                ensure_ascii=False,
            )
        )

    live_root = Path("/ops/apps") / project

    live_check = check_live_matches_after(
        task_id,
        live_root,
        changes=plan["changes"],
    )

    if not live_check["safe"]:
        raise RuntimeError(
            "Live project no longer matches Task After: "
            + json.dumps(
                live_check["conflicts"],
                ensure_ascii=False,
            )
        )

    repo_root = Path(repo_root).resolve()

    if not repo_root.is_dir():
        raise FileNotFoundError(
            f"Git repository does not exist: {repo_root}"
        )

    git_dir = repo_root / ".git"

    if not git_dir.exists():
        raise ValueError(
            f"Not a Git repository: {repo_root}"
        )

    status = subprocess.run(
        [
            "git",
            "-C",
            str(repo_root),
            "status",
            "--porcelain",
        ],
        capture_output=True,
        text=True,
        check=True,
    )

    if status.stdout.strip():
        raise RuntimeError(
            "Git repository is not clean before approval"
        )

    after_root = snapshot_path(
        task_id,
        "after",
    )

    repo_project = (
        repo_root
        / "apps"
        / project
    )

    repo_apps = (
        repo_root
        / "apps"
    ).resolve()

    repo_project_resolved = repo_project.resolve(
        strict=False
    )

    if (
        repo_project_resolved.parent
        != repo_apps
    ):
        raise ValueError(
            "Unsafe repository project path"
        )

    changed_git_paths = []

    # --------------------------------------------------------
    # 先创建 / 恢复目录。
    # --------------------------------------------------------
    directories_to_create = []

    for item in plan["changes"]:
        after_entry = item["after"]

        if (
            after_entry is not None
            and after_entry.get("type")
            == "directory"
        ):
            directories_to_create.append(item)

    for item in sorted(
        directories_to_create,
        key=lambda x: x["path"].count("/"),
    ):
        relative = item["path"]
        destination = repo_project / relative

        destination.mkdir(
            parents=True,
            exist_ok=True,
        )

        os.chmod(
            destination,
            item["after"]["mode"],
        )

    # --------------------------------------------------------
    # ADD / MODIFY 文件：
    # 只从 root-owned After Snapshot 复制。
    # --------------------------------------------------------
    for item in plan["changes"]:
        relative = item["path"]
        after_entry = item["after"]

        git_relative = (
            Path("apps")
            / project
            / relative
        ).as_posix()

        changed_git_paths.append(
            git_relative
        )

        if after_entry is None:
            continue

        if after_entry.get("type") != "file":
            continue

        source = after_root / relative
        destination = repo_project / relative

        source_info = source.lstat()

        if not stat.S_ISREG(source_info.st_mode):
            raise ValueError(
                "Approval source is not a regular file: "
                f"{relative}"
            )

        destination.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        shutil.copy2(
            source,
            destination,
            follow_symlinks=False,
        )

        os.chmod(
            destination,
            after_entry["mode"],
        )

    # --------------------------------------------------------
    # DELETE：
    # 文件先删，目录最后按深度倒序删除。
    # --------------------------------------------------------
    deleted_files = []
    deleted_directories = []

    for item in plan["changes"]:
        if item["after"] is not None:
            continue

        before_entry = item["before"]

        if before_entry.get("type") == "file":
            deleted_files.append(item)
        elif before_entry.get("type") == "directory":
            deleted_directories.append(item)

    for item in deleted_files:
        destination = (
            repo_project / item["path"]
        )

        try:
            info = destination.lstat()
        except FileNotFoundError:
            continue

        if not stat.S_ISREG(info.st_mode):
            raise RuntimeError(
                "Refusing to delete non-regular Git path: "
                f"{item['path']}"
            )

        destination.unlink()

    for item in sorted(
        deleted_directories,
        key=lambda x: x["path"].count("/"),
        reverse=True,
    ):
        destination = (
            repo_project / item["path"]
        )

        try:
            info = destination.lstat()
        except FileNotFoundError:
            continue

        if not stat.S_ISDIR(info.st_mode):
            raise RuntimeError(
                "Refusing to delete non-directory Git path: "
                f"{item['path']}"
            )

        try:
            destination.rmdir()
        except OSError:
            raise RuntimeError(
                "Refusing to delete non-empty Git directory: "
                f"{item['path']}"
            )

    if changed_git_paths:
        subprocess.run(
            [
                "git",
                "-C",
                str(repo_root),
                "add",
                "--all",
                "--",
                *changed_git_paths,
            ],
            check=True,
        )

    diff_check = subprocess.run(
        [
            "git",
            "-C",
            str(repo_root),
            "diff",
            "--cached",
            "--check",
        ],
        capture_output=True,
        text=True,
    )

    if diff_check.returncode != 0:
        raise RuntimeError(
            "git diff --cached --check failed: "
            + diff_check.stdout
            + diff_check.stderr
        )

    # --------------------------------------------------------
    # 二次验证 staging 范围。
    # 即使前面的 apply/stage 逻辑未来出现 bug，
    # 也不允许夹带当前 Task manifest 之外的任何文件。
    # --------------------------------------------------------
    scope_check = validate_staged_scope(
        task_id,
        repo_root,
    )

    staged = subprocess.run(
        [
            "git",
            "-C",
            str(repo_root),
            "diff",
            "--cached",
            "--name-status",
        ],
        capture_output=True,
        text=True,
        check=True,
    )

    return {
        "task_id": task_id,
        "project": project,
        "change_count": plan["change_count"],
        "git_paths": changed_git_paths,
        "staged": staged.stdout.strip(),
        "scope_check": scope_check,
    }


def publish_task_to_git(
    task_id,
    repo_root="/root/my-ops",
):
    """
    正式发布单个 Task 到 GitHub。

    安全顺序：
    1. Task 必须 success + pending
    2. 如果之前 commit 成功但 push 失败，只允许安全重试 push
    3. 新发布必须先 fetch，并确认 HEAD == origin/main
    4. stage_task_for_approval 执行全部 staging 安全检查
    5. commit
    6. push
    7. fetch + 验证 origin/main == commit SHA
    8. 最后才 mark_approved

    push 失败时：
    - approval_status 保持 pending
    - publish_state = push_failed
    - 保存已经生成的 commit SHA
    """
    task_id = validate_task_id(task_id)
    repo_root = Path(repo_root).resolve()

    task = load_task(task_id)

    if task.get("status") != "success":
        raise RuntimeError(
            "Task status must be success"
        )

    if task.get("approval_status") != "pending":
        raise RuntimeError(
            "Task approval status must be pending"
        )

    # --------------------------------------------------------
    # Retry 模式：
    # 上一次已经 commit，但 push 失败。
    # 不允许重新 stage / commit，只重试这个已记录 commit。
    # --------------------------------------------------------
    if task.get("publish_state") == "push_failed":
        commit_sha = task.get("git_commit")

        if not commit_sha:
            raise RuntimeError(
                "push_failed task has no git_commit"
            )

        current_head = subprocess.run(
            [
                "git",
                "-C",
                str(repo_root),
                "rev-parse",
                "HEAD",
            ],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

        if current_head != commit_sha:
            raise RuntimeError(
                "Cannot retry push: HEAD does not match "
                "recorded Task commit"
            )

        commit_message = subprocess.run(
            [
                "git",
                "-C",
                str(repo_root),
                "show",
                "-s",
                "--format=%s",
                commit_sha,
            ],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

        if task_id not in commit_message:
            raise RuntimeError(
                "Cannot retry push: commit does not belong "
                "to this Task"
            )

        try:
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(repo_root),
                    "push",
                    "origin",
                    "main",
                ],
                capture_output=True,
                text=True,
                check=True,
            )
        except subprocess.CalledProcessError as exc:
            mark_publish_state(
                task,
                "push_failed",
                git_commit=commit_sha,
                error="git push failed",
            )
            raise RuntimeError(
                "Git push failed; Task remains pending"
            ) from exc

        subprocess.run(
            [
                "git",
                "-C",
                str(repo_root),
                "fetch",
                "origin",
                "main",
            ],
            capture_output=True,
            text=True,
            check=True,
        )

        origin_head = subprocess.run(
            [
                "git",
                "-C",
                str(repo_root),
                "rev-parse",
                "origin/main",
            ],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

        if origin_head != commit_sha:
            raise RuntimeError(
                "Push completed but origin/main verification failed"
            )

        task = load_task(task_id)
        mark_publish_state(
            task,
            "pushed",
            git_commit=commit_sha,
        )

        task = load_task(task_id)
        mark_approved(
            task,
            git_commit=commit_sha,
        )

        return {
            "task_id": task_id,
            "project": task["project"],
            "commit": commit_sha,
            "retry": True,
            "approved": True,
        }

    # --------------------------------------------------------
    # 新发布。
    # Git 仓库必须从与 origin/main 完全一致的状态开始。
    # --------------------------------------------------------
    validate_git_publish_state(
        repo_root,
        fetch=True,
    )

    mark_publish_state(
        task,
        "staging",
    )

    try:
        stage_result = stage_task_for_approval(
            task_id,
            repo_root,
        )
    except Exception:
        task = load_task(task_id)
        clear_publish_state(task)
        raise

    # staging 完成后再次执行范围校验。
    validate_staged_scope(
        task_id,
        repo_root,
    )

    commit_message = (
        f"task: approve {task['project']} {task_id}"
    )

    try:
        subprocess.run(
            [
                "git",
                "-C",
                str(repo_root),
                "commit",
                "-m",
                commit_message,
            ],
            capture_output=True,
            text=True,
            check=True,
        )
    except subprocess.CalledProcessError as exc:
        task = load_task(task_id)
        clear_publish_state(task)

        raise RuntimeError(
            "Git commit failed; Task remains pending"
        ) from exc

    commit_sha = subprocess.run(
        [
            "git",
            "-C",
            str(repo_root),
            "rev-parse",
            "HEAD",
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()

    task = load_task(task_id)
    mark_publish_state(
        task,
        "committed",
        git_commit=commit_sha,
    )

    try:
        subprocess.run(
            [
                "git",
                "-C",
                str(repo_root),
                "push",
                "origin",
                "main",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
    except subprocess.CalledProcessError as exc:
        task = load_task(task_id)

        mark_publish_state(
            task,
            "push_failed",
            git_commit=commit_sha,
            error="git push failed",
        )

        raise RuntimeError(
            "Git commit succeeded but push failed; "
            "Task remains pending and can retry push"
        ) from exc

    subprocess.run(
        [
            "git",
            "-C",
            str(repo_root),
            "fetch",
            "origin",
            "main",
        ],
        capture_output=True,
        text=True,
        check=True,
    )

    origin_head = subprocess.run(
        [
            "git",
            "-C",
            str(repo_root),
            "rev-parse",
            "origin/main",
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()

    if origin_head != commit_sha:
        task = load_task(task_id)

        mark_publish_state(
            task,
            "push_failed",
            git_commit=commit_sha,
            error="origin/main verification failed",
        )

        raise RuntimeError(
            "Push verification failed; Task remains pending"
        )

    task = load_task(task_id)

    mark_publish_state(
        task,
        "pushed",
        git_commit=commit_sha,
    )

    task = load_task(task_id)

    mark_approved(
        task,
        git_commit=commit_sha,
    )

    return {
        "task_id": task_id,
        "project": task["project"],
        "commit": commit_sha,
        "retry": False,
        "approved": True,
        "staged": stage_result["staged"],
    }


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


def check_live_matches_after(
    task_id,
    project_path,
    changes=None,
):
    """
    Approval 安全检查：

    验证当前 Live 项目中待发布路径是否仍然等于
    immutable Task After Snapshot。

    - 普通增量发布：默认检查 Before -> After manifest。
    - 首次完整发布：调用方传入完整 Approval plan，
      检查所有实际待发布路径。

    Reject 继续独立使用 check_reject_conflicts()。
    """
    task_id = validate_task_id(task_id)

    if changes is None:
        changes = build_change_manifest(task_id)

    project_root = Path(project_path).resolve()

    if not project_root.exists():
        raise FileNotFoundError(
            f"Project does not exist: {project_root}"
        )

    if not project_root.is_dir():
        raise NotADirectoryError(
            f"Project is not a directory: {project_root}"
        )

    conflicts = []

    for item in changes:
        relative = validate_approval_relative_path(
            item["path"]
        )

        expected_after = item["after"]
        live_path = project_root / relative
        live_state = inspect_live_path(live_path)

        if expected_after is None:
            if live_state is not None:
                conflicts.append(
                    {
                        "path": relative,
                        "reason": (
                            "path_exists_but_task_after_deleted_it"
                        ),
                    }
                )
            continue

        if live_state is None:
            conflicts.append(
                {
                    "path": relative,
                    "reason": "live_path_missing",
                }
            )
            continue

        if live_state.get("type") == "unsupported":
            conflicts.append(
                {
                    "path": relative,
                    "reason": "unsupported_live_path",
                }
            )
            continue

        if live_state.get("type") != expected_after.get("type"):
            conflicts.append(
                {
                    "path": relative,
                    "reason": "path_type_changed",
                }
            )
            continue

        if expected_after.get("type") == "file":
            if (
                live_state.get("sha256")
                != expected_after.get("sha256")
            ):
                conflicts.append(
                    {
                        "path": relative,
                        "reason": "file_content_changed",
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
