import re
import shutil
import asyncio
import json
import logging
import os
from pathlib import Path

from dotenv import load_dotenv
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)

from codex_runner import (
    run_codex,
    validate_workdir,
)

from quota import (
    build_usage_text,
    get_rate_limits,
)

from task_manager import (
    TASK_LOCK,
    build_task_diff,
    create_snapshot,
    create_task,
    load_task,
    mark_failed,
    mark_running,
    mark_success,
    reject_task_changes,
    publish_task_to_git,
)


# ============================================================
# 基础配置
# ============================================================

ENV_FILE = "/ops/conf/tg-codex-agent/.env"

load_dotenv(ENV_FILE)

BOT_TOKEN = os.getenv(
    "TG_BOT_TOKEN",
    "",
).strip()

ALLOWED_USER_IDS = {
    int(x.strip())
    for x in os.getenv(
        "TG_ALLOWED_USER_IDS",
        "",
    ).split(",")
    if x.strip()
}

QUOTA_ALERT_THRESHOLD = int(
    os.getenv(
        "QUOTA_ALERT_THRESHOLD",
        "10",
    )
)

QUOTA_CHECK_INTERVAL = int(
    os.getenv(
        "QUOTA_CHECK_INTERVAL",
        "3600",
    )
)

WORK_ROOT = Path(
    os.getenv(
        "CODEX_WORK_ROOT",
        "/ops/apps",
    )
).resolve()

DATA_DIR = Path(
    "/ops/apps/tg-codex-agent/data"
)

ALERT_STATE_FILE = (
    DATA_DIR / "quota_alert_state.json"
)


# ============================================================
# 日志
# ============================================================

logging.basicConfig(
    format=(
        "%(asctime)s | %(levelname)s | "
        "%(name)s | %(message)s"
    ),
    level=logging.INFO,
)

logger = logging.getLogger(
    "tg-codex-agent"
)

# 防止 Telegram Bot Token 出现在 httpx INFO 日志中
logging.getLogger(
    "httpx"
).setLevel(
    logging.WARNING
)


# ============================================================
# 权限
# ============================================================

def is_allowed(update: Update) -> bool:
    user = update.effective_user
    chat = update.effective_chat

    if not user or not chat:
        return False

    # V1 仅允许私聊
    if chat.type != "private":
        return False

    return user.id in ALLOWED_USER_IDS


async def deny(update: Update):
    if update.effective_message:
        await update.effective_message.reply_text(
            "⛔ 无权限访问此系统。"
        )


# ============================================================
# 告警状态文件
# ============================================================

def default_alert_state():
    return {
        "primary_alerted": False,
        "secondary_alerted": False,
    }


def load_alert_state():
    if not ALERT_STATE_FILE.exists():
        return default_alert_state()

    try:
        with ALERT_STATE_FILE.open(
            "r",
            encoding="utf-8",
        ) as f:
            state = json.load(f)

        return {
            "primary_alerted": bool(
                state.get(
                    "primary_alerted",
                    False,
                )
            ),
            "secondary_alerted": bool(
                state.get(
                    "secondary_alerted",
                    False,
                )
            ),
        }

    except Exception:
        logger.exception(
            "读取额度告警状态失败"
        )

        return default_alert_state()


def save_alert_state(state):
    DATA_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    temp_file = ALERT_STATE_FILE.with_suffix(
        ".tmp"
    )

    with temp_file.open(
        "w",
        encoding="utf-8",
    ) as f:
        json.dump(
            state,
            f,
            ensure_ascii=False,
            indent=2,
        )

    temp_file.replace(
        ALERT_STATE_FILE
    )


# ============================================================
# Codex 额度
# ============================================================

def calculate_quota():
    (
        rate_limits,
        _session_file,
        _snapshot_timestamp,
    ) = get_rate_limits(
        refresh_if_stale=True
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

    return {
        "plan": plan,
        "primary_used": primary_used,
        "primary_left": primary_left,
        "secondary_used": secondary_used,
        "secondary_left": secondary_left,
    }


# ============================================================
# TG 主动通知
# ============================================================

async def send_to_allowed_users(
    application,
    text,
):
    for user_id in ALLOWED_USER_IDS:
        try:
            await application.bot.send_message(
                chat_id=user_id,
                text=text,
            )

        except Exception:
            logger.exception(
                "发送 Telegram 通知失败 user_id=%s",
                user_id,
            )


# ============================================================
# 后台额度巡检
# ============================================================

async def quota_monitor(context):
    logger.info(
        "开始执行 Codex 额度巡检"
    )

    try:
        quota = calculate_quota()

    except Exception:
        logger.exception(
            "Codex 额度巡检失败"
        )
        return

    state = load_alert_state()

    primary_left = quota[
        "primary_left"
    ]

    secondary_left = quota[
        "secondary_left"
    ]

    changed = False
    alert_messages = []

    # --------------------------------------------------------
    # 5小时额度
    # --------------------------------------------------------

    if (
        primary_left
        <= QUOTA_ALERT_THRESHOLD
    ):
        if not state[
            "primary_alerted"
        ]:
            alert_messages.append(
                "⏱ 5小时额度不足\n"
                f"剩余：{primary_left:g}%"
            )

            state[
                "primary_alerted"
            ] = True

            changed = True

    else:
        if state[
            "primary_alerted"
        ]:
            state[
                "primary_alerted"
            ] = False

            changed = True

            logger.info(
                "5小时额度已恢复，解除告警状态"
            )

    # --------------------------------------------------------
    # 周额度
    # --------------------------------------------------------

    if (
        secondary_left
        <= QUOTA_ALERT_THRESHOLD
    ):
        if not state[
            "secondary_alerted"
        ]:
            alert_messages.append(
                "📅 周额度不足\n"
                f"剩余：{secondary_left:g}%"
            )

            state[
                "secondary_alerted"
            ] = True

            changed = True

    else:
        if state[
            "secondary_alerted"
        ]:
            state[
                "secondary_alerted"
            ] = False

            changed = True

            logger.info(
                "周额度已恢复，解除告警状态"
            )

    # --------------------------------------------------------
    # 保存状态
    # --------------------------------------------------------

    if changed:
        save_alert_state(
            state
        )

    # --------------------------------------------------------
    # 发送告警
    # --------------------------------------------------------

    if alert_messages:
        text = (
            "⚠️ Codex 额度告警\n\n"
            f"套餐：{quota['plan'].capitalize()}\n\n"
            + "\n\n".join(
                alert_messages
            )
            + "\n\n"
            f"告警阈值："
            f"{QUOTA_ALERT_THRESHOLD}%\n\n"
            "请检查 Codex 额度或切换账号。"
        )

        await send_to_allowed_users(
            context.application,
            text,
        )

        logger.warning(
            "Codex 额度达到告警阈值"
        )

    else:
        logger.info(
            "Codex 额度正常：5h=%g%% weekly=%g%%",
            primary_left,
            secondary_left,
        )


# ============================================================
# Codex Task 后台执行
# ============================================================

async def execute_codex_task(
    application,
    chat_id,
    task,
):
    task_id = task["task_id"]
    project = task["project"]

    workdir = (
        WORK_ROOT / project
    )

    logger.info(
        "Task 开始等待执行 task_id=%s project=%s",
        task_id,
        project,
    )

    try:
        # V1 全局只允许一个 Codex 开发任务运行
        async with TASK_LOCK:
            logger.info(
                "Task 开始执行 task_id=%s project=%s",
                task_id,
                project,
            )

            # 新项目允许直接通过 Telegram 创建。
            # 创建动作必须位于 TASK_LOCK 内，避免并发 Task
            # 同时初始化同一个项目。
            if not workdir.exists():
                workdir.mkdir(
                    mode=0o755,
                    parents=False,
                    exist_ok=False,
                )

                shutil.chown(
                    workdir,
                    user="codex-runner",
                    group="codex-runner",
                )

                logger.info(
                    "已创建新项目目录 task_id=%s project=%s path=%s",
                    task_id,
                    project,
                    workdir,
                )

            # 无论新项目还是已有项目，都重新走正式路径校验。
            workdir = validate_workdir(
                workdir
            )

            # Codex 执行前保存项目完整基线。
            # 后续 /diff、/reject、/approve 都以该快照为准。
            create_snapshot(
                task_id,
                workdir,
                "before",
            )
            task["snapshot_before"] = True

            mark_running(
                task
            )

            await application.bot.send_message(
                chat_id=chat_id,
                text=(
                    "🚀 Codex 开始执行\n\n"
                    f"Task：{task_id}\n"
                    f"项目：{project}"
                ),
            )

            # run_codex 是同步阻塞函数。
            # 放入线程，避免阻塞 Telegram Bot event loop。
            result = await asyncio.to_thread(
                run_codex,
                workdir,
                task["prompt"],
            )

            # Codex 成功返回后保存修改后的项目状态。
            create_snapshot(
                task_id,
                workdir,
                "after",
            )
            task["snapshot_after"] = True

            mark_success(
                task,
                result,
            )

            logger.info(
                "Task 执行成功 task_id=%s",
                task_id,
            )

            # Telegram 单条文本存在长度限制，
            # V1 先控制结果长度，完整结果保存在 Task JSON。
            max_result_length = 3200

            display_result = result

            if (
                len(display_result)
                > max_result_length
            ):
                display_result = (
                    display_result[
                        :max_result_length
                    ]
                    + "\n\n……结果过长，已截断。"
                )

            await application.bot.send_message(
                chat_id=chat_id,
                text=(
                    "✅ Codex Task 执行完成\n\n"
                    f"Task：{task_id}\n"
                    f"项目：{project}\n\n"
                    "执行结果：\n"
                    f"{display_result}\n\n"
                    "当前尚未执行 Git commit / push。"
                ),
            )

    except Exception as exc:
        logger.exception(
            "Task 执行失败 task_id=%s",
            task_id,
        )

        mark_failed(
            task,
            exc,
        )

        error_text = str(
            exc
        )

        if len(error_text) > 2500:
            error_text = (
                error_text[:2500]
                + "\n……错误信息已截断。"
            )

        try:
            await application.bot.send_message(
                chat_id=chat_id,
                text=(
                    "❌ Codex Task 执行失败\n\n"
                    f"Task：{task_id}\n"
                    f"项目：{project}\n\n"
                    f"错误：\n{error_text}"
                ),
            )

        except Exception:
            logger.exception(
                "发送 Task 失败通知失败 task_id=%s",
                task_id,
            )


# ============================================================
# /start
# ============================================================

async def start_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not is_allowed(update):
        await deny(update)
        return

    await update.message.reply_text(
        "🤖 TG-Codex Agent 已连接\n\n"
        "Codex：受限执行模式已启用\n"
        "执行用户：codex-runner\n"
        "工作根目录：/ops/apps\n\n"
        "可用命令：\n"
        "/status - 查看系统状态\n"
        "/usage - 查看 Codex 额度\n"
        "/task 项目名 开发需求 - 创建/继续 Codex 开发任务\n"
        "/task_status Task_ID - 查看任务状态\n"
        "/diff Task_ID - 查看任务变更\n"
        "/approve Task_ID - 审批并发布到 GitHub\n"
        "/reject Task_ID - 拒绝并安全回滚\n"
        "/whoami - 查看当前 Telegram User ID\n\n"
        "项目名示例：public-vpn-node\n"
        "不要输入 < > 尖括号。"
    )


# ============================================================
# /status
# ============================================================

async def status_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not is_allowed(update):
        await deny(update)
        return

    task_state = (
        "执行中"
        if TASK_LOCK.locked()
        else "空闲"
    )

    await update.message.reply_text(
        "🟢 TG-Codex Agent 状态正常\n\n"
        "Telegram：已连接\n"
        "白名单：验证通过\n"
        "Codex：受限执行已启用\n"
        "Codex 用户：codex-runner\n"
        "Codex Task："
        f"{task_state}\n"
        "额度查询：已启用\n"
        "额度巡检：已启用\n"
        f"巡检周期：{QUOTA_CHECK_INTERVAL} 秒\n"
        f"告警阈值：{QUOTA_ALERT_THRESHOLD}%\n"
        "GitHub：待接入"
    )


# ============================================================
# /usage
# ============================================================

async def usage_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not is_allowed(update):
        await deny(update)
        return

    try:
        text = build_usage_text(
            threshold=QUOTA_ALERT_THRESHOLD,
            refresh_if_stale=True,
        )

        await update.message.reply_text(
            text
        )

    except Exception:
        logger.exception(
            "读取 Codex 额度失败"
        )

        await update.message.reply_text(
            "❌ Codex 额度读取失败，请检查服务日志。"
        )


# ============================================================
# /task
# ============================================================

async def task_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not is_allowed(update):
        await deny(update)
        return

    if len(context.args) < 2:
        await update.message.reply_text(
            "用法：\n"
            "/task <项目名> <开发需求>\n\n"
            "示例：\n"
            "/task codex-test 创建一个 Python hello world"
        )
        return

    project = (
        context.args[0]
        .strip()
    )

    prompt = (
        " ".join(
            context.args[1:]
        )
        .strip()
    )

    if not project or not prompt:
        await update.message.reply_text(
            "❌ 项目名和开发需求不能为空。"
        )
        return

    # 项目名只允许安全的一级目录名称。
    # 新项目可以不存在，真正创建动作在 TASK_LOCK 内执行。
    if not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9._-]*",
        project,
    ):
        await update.message.reply_text(
            "❌ 项目名无效。\n\n"
            "只允许英文、数字、点、下划线、短横线，"
            "并且必须以英文或数字开头。\n"
            "例如：public-vpn-node"
        )
        return

    if project == "tg-codex-agent":
        await update.message.reply_text(
            "❌ 该项目属于 TG-Codex Agent 自身，"
            "禁止通过 Codex Task 修改。"
        )
        return

    task = create_task(
        user_id=update.effective_user.id,
        project=project,
        prompt=prompt,
    )

    task_id = task[
        "task_id"
    ]

    logger.info(
        "收到新 Task task_id=%s user_id=%s project=%s",
        task_id,
        update.effective_user.id,
        project,
    )

    if TASK_LOCK.locked():
        queue_text = (
            "当前已有 Codex Task 正在执行。\n"
            "本任务已创建，将等待前一个任务完成。"
        )
    else:
        queue_text = (
            "当前执行器空闲，任务即将开始。"
        )

    await update.message.reply_text(
        "📥 Codex Task 已接收\n\n"
        f"Task：{task_id}\n"
        f"项目：{project}\n"
        f"状态：等待执行\n\n"
        f"{queue_text}"
    )

    # 后台启动，不阻塞 Telegram handler。
    asyncio.create_task(
        execute_codex_task(
            context.application,
            update.effective_chat.id,
            task,
        ),
        name=f"codex-task-{task_id}",
    )


# ============================================================
# /whoami
# ============================================================

async def task_status_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not is_allowed(update):
        await deny(update)
        return

    if len(context.args) != 1:
        await update.message.reply_text(
            "用法：/task_status <task_id>"
        )
        return

    task_id = context.args[0].strip()
    task = load_task(task_id)

    if not task:
        await update.message.reply_text(
            f"❌ Task 不存在：{task_id}"
        )
        return

    status = task.get("status", "unknown")
    approval_status = task.get(
        "approval_status",
        "pending",
    )

    snapshot_before = (
        "✅"
        if task.get("snapshot_before")
        else "❌"
    )

    snapshot_after = (
        "✅"
        if task.get("snapshot_after")
        else "❌"
    )

    git_commit = task.get("git_commit") or "-"

    await update.message.reply_text(
        "📋 Task 状态\n\n"
        f"Task：{task_id}\n"
        f"项目：{task.get('project', '-')}\n"
        f"执行状态：{status}\n"
        f"审批状态：{approval_status}\n\n"
        f"创建时间：{task.get('created_at') or '-'}\n"
        f"开始时间：{task.get('started_at') or '-'}\n"
        f"完成时间：{task.get('finished_at') or '-'}\n\n"
        f"Before Snapshot：{snapshot_before}\n"
        f"After Snapshot：{snapshot_after}\n"
        f"Git Commit：{git_commit}"
    )


async def diff_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not is_allowed(update):
        await deny(update)
        return

    if len(context.args) != 1:
        await update.message.reply_text(
            "用法：/diff <task_id>"
        )
        return

    task_id = context.args[0].strip()
    task = load_task(task_id)

    if not task:
        await update.message.reply_text(
            f"❌ Task 不存在：{task_id}"
        )
        return

    if not task.get("snapshot_before"):
        await update.message.reply_text(
            "❌ 该 Task 没有 Before Snapshot，"
            "无法生成 Diff。"
        )
        return

    if not task.get("snapshot_after"):
        await update.message.reply_text(
            "❌ 该 Task 没有 After Snapshot，"
            "无法生成 Diff。"
        )
        return

    try:
        diff_text = await asyncio.to_thread(
            build_task_diff,
            task_id,
        )
    except Exception as exc:
        logger.exception(
            "生成 Task Diff 失败 task_id=%s",
            task_id,
        )

        await update.message.reply_text(
            "❌ 生成 Diff 失败\n\n"
            f"Task：{task_id}\n"
            f"错误：{str(exc)[:2000]}"
        )
        return

    if not diff_text.strip():
        await update.message.reply_text(
            "ℹ️ 该 Task 没有文件差异。\n\n"
            f"Task：{task_id}"
        )
        return

    max_diff_length = 3400

    display_diff = diff_text

    if len(display_diff) > max_diff_length:
        display_diff = (
            display_diff[:max_diff_length]
            + "\n\n……Diff 过长，已截断。"
        )

    await update.message.reply_text(
        "🧾 Task Diff\n\n"
        f"Task：{task_id}\n"
        f"项目：{task.get('project', '-')}\n\n"
        f"{display_diff}"
    )


async def approve_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not is_allowed(update):
        await deny(update)
        return

    if len(context.args) != 1:
        await update.message.reply_text(
            "用法：/approve <task_id>"
        )
        return

    task_id = context.args[0].strip()

    try:
        task = load_task(task_id)
    except ValueError:
        await update.message.reply_text(
            "❌ Task ID 格式无效。"
        )
        return

    if not task:
        await update.message.reply_text(
            f"❌ Task 不存在：{task_id}"
        )
        return

    if task.get("status") != "success":
        await update.message.reply_text(
            "❌ 只有执行成功的 Task 才能 Approve。\n\n"
            f"Task：{task_id}\n"
            f"当前状态：{task.get('status', 'unknown')}"
        )
        return

    if task.get("approval_status", "pending") != "pending":
        await update.message.reply_text(
            "❌ 该 Task 已经处理过。\n\n"
            f"Task：{task_id}\n"
            "审批状态："
            f"{task.get('approval_status', 'unknown')}"
        )
        return

    project = task.get("project")

    if not project:
        await update.message.reply_text(
            "❌ Task 缺少项目名称，无法 Approve。"
        )
        return

    # 与 /task 和 /reject 共用全局锁。
    # 发布期间禁止其它 Task 同时改变 Live 项目或 Git 发布状态。
    await update.message.reply_text(
        "⏳ 正在执行安全审批并发布到 GitHub...\n\n"
        f"Task：{task_id}\n"
        f"项目：{project}\n\n"
        "发布完成前请勿重复提交 Approve。"
    )

    try:
        async with TASK_LOCK:
            result = await asyncio.to_thread(
                publish_task_to_git,
                task_id,
            )

    except RuntimeError as exc:
        logger.warning(
            "Approve 被拒绝或发布失败 task_id=%s error=%s",
            task_id,
            exc,
        )

        # 不把内部 Git stderr / secret 等详细内容直接发到 TG。
        # publish_task_to_git 会确保失败时不会错误标记 approved。
        await update.message.reply_text(
            "⚠️ Approve 未完成。\n\n"
            f"Task：{task_id}\n"
            f"项目：{project}\n"
            f"原因：{str(exc)[:1000]}\n\n"
            "Task 不会因为本次失败被错误标记为 approved。"
        )
        return

    except Exception:
        logger.exception(
            "Approve 失败 task_id=%s",
            task_id,
        )

        await update.message.reply_text(
            "❌ Approve 发生异常。\n\n"
            f"Task：{task_id}\n"
            f"项目：{project}\n\n"
            "请检查服务器日志。Task 不会被自动标记为 approved。"
        )
        return

    retry_text = (
        "是（恢复之前失败的 Push）"
        if result.get("retry")
        else "否"
    )

    await update.message.reply_text(
        "✅ Task 已 Approve 并发布到 GitHub\n\n"
        f"Task：{task_id}\n"
        f"项目：{project}\n"
        f"Commit：{result['commit']}\n"
        "审批状态：approved\n"
        f"Push 重试：{retry_text}"
    )


async def reject_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not is_allowed(update):
        await deny(update)
        return

    if len(context.args) != 1:
        await update.message.reply_text(
            "用法：/reject <task_id>"
        )
        return

    task_id = context.args[0].strip()

    try:
        task = load_task(task_id)
    except ValueError:
        await update.message.reply_text(
            "❌ Task ID 格式无效。"
        )
        return

    if not task:
        await update.message.reply_text(
            f"❌ Task 不存在：{task_id}"
        )
        return

    if task.get("status") != "success":
        await update.message.reply_text(
            "❌ 只有执行成功的 Task 才能 Reject。\n\n"
            f"Task：{task_id}\n"
            f"当前状态：{task.get('status', 'unknown')}"
        )
        return

    if task.get("approval_status", "pending") != "pending":
        await update.message.reply_text(
            "❌ 该 Task 已经处理过。\n\n"
            f"Task：{task_id}\n"
            "审批状态："
            f"{task.get('approval_status', 'unknown')}"
        )
        return

    project = task.get("project")

    if not project:
        await update.message.reply_text(
            "❌ Task 缺少项目名称，无法 Reject。"
        )
        return

    workdir = WORK_ROOT / project

    try:
        workdir = validate_workdir(workdir)
    except Exception as exc:
        logger.warning(
            "Reject 项目路径验证失败 task_id=%s project=%s error=%s",
            task_id,
            project,
            exc,
        )

        await update.message.reply_text(
            "❌ 项目路径验证失败，拒绝执行 Reject。\n\n"
            f"Task：{task_id}\n"
            f"项目：{project}"
        )
        return

    await update.message.reply_text(
        "⏳ 正在检查并回滚 Task...\n\n"
        f"Task：{task_id}\n"
        f"项目：{project}"
    )

    try:
        # 与 Codex Task 共用全局锁。
        # Reject 期间不允许新的 Codex Task 同时修改项目。
        async with TASK_LOCK:
            result = await asyncio.to_thread(
                reject_task_changes,
                task_id,
                workdir,
            )

    except RuntimeError as exc:
        logger.warning(
            "Reject 冲突 task_id=%s error=%s",
            task_id,
            exc,
        )

        await update.message.reply_text(
            "⚠️ Reject 已拒绝执行。\n\n"
            f"Task：{task_id}\n"
            "检测到 Task 完成后相关文件又发生变化。\n"
            "为避免覆盖后续修改，没有改动任何文件。"
        )
        return

    except Exception as exc:
        logger.exception(
            "Reject 失败 task_id=%s",
            task_id,
        )

        await update.message.reply_text(
            "❌ Reject 失败。\n\n"
            f"Task：{task_id}\n"
            f"错误：{str(exc)[:1500]}"
        )
        return

    await update.message.reply_text(
        "↩️ Task 已 Reject 并完成安全回滚\n\n"
        f"Task：{task_id}\n"
        f"项目：{project}\n"
        f"回滚变更：{result['change_count']} 项\n"
        "审批状态：rejected\n\n"
        "未执行 Git commit / push。"
    )


async def whoami_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not is_allowed(update):
        await deny(update)
        return

    await update.message.reply_text(
        "Telegram User ID："
        f"{update.effective_user.id}"
    )


# ============================================================
# Main
# ============================================================

def main():
    if not BOT_TOKEN:
        raise RuntimeError(
            "TG_BOT_TOKEN 未配置"
        )

    if not ALLOWED_USER_IDS:
        raise RuntimeError(
            "TG_ALLOWED_USER_IDS 未配置"
        )

    if QUOTA_CHECK_INTERVAL < 60:
        raise RuntimeError(
            "QUOTA_CHECK_INTERVAL 不能小于 60 秒"
        )

    DATA_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    logger.info(
        "Starting TG-Codex Agent, allowed users=%d",
        len(ALLOWED_USER_IDS),
    )

    logger.info(
        "Quota monitor interval=%s threshold=%s%%",
        QUOTA_CHECK_INTERVAL,
        QUOTA_ALERT_THRESHOLD,
    )

    logger.info(
        "Codex restricted executor enabled, work_root=%s",
        WORK_ROOT,
    )

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .build()
    )

    app.add_handler(
        CommandHandler(
            "start",
            start_command,
        )
    )

    app.add_handler(
        CommandHandler(
            "status",
            status_command,
        )
    )

    app.add_handler(
        CommandHandler(
            "usage",
            usage_command,
        )
    )

    app.add_handler(
        CommandHandler(
            "task",
            task_command,
        )
    )

    app.add_handler(
        CommandHandler(
            "task_status",
            task_status_command,
        )
    )

    app.add_handler(
        CommandHandler(
            "diff",
            diff_command,
        )
    )

    app.add_handler(
        CommandHandler(
            "approve",
            approve_command,
        )
    )

    app.add_handler(
        CommandHandler(
            "reject",
            reject_command,
        )
    )

    app.add_handler(
        CommandHandler(
            "whoami",
            whoami_command,
        )
    )

    # 启动后 30 秒执行第一次额度巡检
    app.job_queue.run_repeating(
        quota_monitor,
        interval=QUOTA_CHECK_INTERVAL,
        first=30,
        name="codex-quota-monitor",
    )

    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()
