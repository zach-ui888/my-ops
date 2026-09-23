# TG-Codex Agent

Telegram 控制的 Codex 开发与 GitHub 审批发布系统。

## 1. 系统架构

系统分为两个权限层：

- Controller
  - 由 systemd 以 root 身份运行。
  - 接收 Telegram 指令。
  - 管理 Task、Snapshot、审批和 Git 发布。
  - GitHub SSH 凭证仅由 root 持有。

- Codex Runner
  - 使用独立用户 `codex-runner`。
  - 实际 Codex 开发任务由该用户执行。
  - 不拥有 sudo/root 权限。
  - 无法读取 `/ops/conf/tg-codex-agent`。
  - 无法读取 `/root/.ssh`。
  - 不拥有 GitHub 发布凭证。

工作目录：

- `/ops/apps/<project>`
  - Codex 项目工作区。

- `/ops/apps/tg-codex-agent`
  - Agent 正式运行目录。

- `/ops/conf/tg-codex-agent/.env`
  - Agent 私密配置。

- `/root/my-ops`
  - GitHub 发布仓库。

- `/ops/apps/tg-codex-agent/data/tasks`
  - Task 运行状态。

- `/ops/apps/tg-codex-agent/data/snapshots`
  - Task Before / After Snapshot。

## 2. Telegram 指令

主要指令：

- `/status`
  - 查看 Agent 状态。

- `/usage`
  - 查看 Codex 使用额度。

- `/task 项目名 开发需求`
  - 创建或继续 Codex 开发任务。

- `/task_status Task_ID`
  - 查看任务状态。

- `/diff Task_ID`
  - 查看任务文件变化。

- `/approve Task_ID`
  - 审批任务并发布到 GitHub。

- `/reject Task_ID`
  - 拒绝任务并安全回滚。

- `/whoami`
  - 查看 Telegram User ID。

不要在项目名或 Task ID 外添加 `< >` 尖括号。

## 3. Git 发布安全模型

Codex Runner 不允许直接向 GitHub push。

正常发布流程：

1. Telegram 创建 Task。
2. Controller 创建 Before Snapshot。
3. `codex-runner` 执行 Codex。
4. Controller 创建 After Snapshot。
5. 用户使用 `/diff` 检查变更。
6. 用户使用 `/approve Task_ID`。
7. Controller 校验 Task、Snapshot、Live Workspace 和 Git 状态。
8. Controller执行 Secret Scan。
9. 仅将该 Task 的允许变更写入 `/root/my-ops`。
10. 仅 stage 当前 Task 的文件。
11. Git commit。
12. Git push `origin/main`。
13. 验证远端 commit。
14. Task 标记为 approved。

`tg-codex-agent` 自身是受保护项目，不能通过普通 `/task` 修改。

## 4. 配置

配置文件：

`/ops/conf/tg-codex-agent/.env`

Git 仓库只保存：

`.env.example`

绝对不要将真实 `.env` 提交 GitHub。

主要配置：

- `TG_BOT_TOKEN`
- `TG_ALLOWED_USER_IDS`
- `CODEX_BIN`
- `CODEX_HOME`
- `CODEX_RUNNER_USER`
- `CODEX_RUNNER_HOME`
- `CODEX_RUNNER_BIN`
- `CODEX_WORK_ROOT`
- `CODEX_EXEC_TIMEOUT`
- `QUOTA_ALERT_THRESHOLD`
- `QUOTA_CHECK_INTERVAL`
- `QUOTA_SNAPSHOT_MAX_AGE`
- `TZ`

## 5. Python 环境

当前正式环境：

- Ubuntu 22.04
- Python 3.10
- 独立 venv：
  `/ops/apps/tg-codex-agent/venv`

依赖版本保存在：

`requirements.txt`

## 6. systemd

正式服务：

`tg-codex-agent.service`

Git 模板：

`deploy/tg-codex-agent.service`

正式安装位置：

`/etc/systemd/system/tg-codex-agent.service`

检查：

    systemctl status tg-codex-agent

开机自启：

    systemctl is-enabled tg-codex-agent

日志：

    journalctl -u tg-codex-agent -f

## 7. 新服务器恢复

### 第一步：准备 GitHub

在新服务器配置 root 的 GitHub SSH Key。

私钥不能存储在本仓库。

确认：

    ssh -T git@github.com

然后 clone：

    git clone git@github.com:zach-ui888/my-ops.git /root/my-ops

### 第二步：初始化基础目录

执行：

    cd /root/my-ops
    ./bootstrap-init.sh

### 第三步：安装 TG-Codex Agent

执行：

    cd /root/my-ops/apps/tg-codex-agent
    ./deploy/install.sh

该脚本会：

- 安装系统依赖。
- 创建 `codex-runner`。
- 创建 `/ops` 目录。
- 设置 Task/Snapshot/Config 权限。
- 安装 Agent 源码。
- 创建 Python venv。
- 安装 Python requirements。
- 安装 systemd unit。
- 设置 systemd 开机自启。
- 创建 `.env` 模板（仅当真实 `.env` 不存在）。

安装脚本不会自动恢复任何 Secret。

### 第四步：恢复 Telegram 配置

编辑：

    /ops/conf/tg-codex-agent/.env

至少需要正确配置：

- `TG_BOT_TOKEN`
- `TG_ALLOWED_USER_IDS`

权限必须为：

    chmod 600 /ops/conf/tg-codex-agent/.env

配置目录必须保持 root-only：

    chmod 700 /ops/conf/tg-codex-agent

### 第五步：恢复 Codex

需要分别恢复：

- root Codex
- `codex-runner` Codex

Runner CLI 目标路径：

    /home/codex-runner/.local/bin/codex

Root CLI 目标路径：

    /root/.local/bin/codex

Codex 登录凭证不能提交到 GitHub。

必须分别完成安装和登录。

Runner 验证：

    runuser -u codex-runner -- \
      /home/codex-runner/.local/bin/codex --version

Root 验证：

    /root/.local/bin/codex --version

### 第六步：验证 GitHub 发布权限

GitHub SSH 私钥只允许 root 持有。

验证：

    ssh -T git@github.com

确认仓库：

    cd /root/my-ops
    git remote -v

`codex-runner` 不应该拥有 root GitHub SSH 私钥。

### 第七步：启动 Agent

执行：

    systemctl restart tg-codex-agent

检查：

    systemctl status tg-codex-agent --no-pager

### 第八步：健康检查

执行：

    cd /root/my-ops/apps/tg-codex-agent
    ./deploy/health-check.sh

预期：

    RESULT: HEALTHY

然后在 Telegram 测试：

- `/status`
- `/usage`
- `/whoami`

## 8. GitHub 不保存的内容

以下内容故意不进入 Git：

- Telegram Bot Token
- 真实 `.env`
- GitHub SSH 私钥
- Codex 登录凭证
- Codex Session
- Python venv
- Task Runtime Data
- Snapshot Runtime Data
- Logs
- 数据库文件
- 临时文件
- Backup 文件

这些内容不能因为方便恢复而直接提交 GitHub。

## 9. Task / Snapshot 灾备

GitHub 保存程序和部署配置，但默认不保存：

`/ops/apps/tg-codex-agent/data/tasks`

以及：

`/ops/apps/tg-codex-agent/data/snapshots`

因此：

- GitHub 可以恢复 Agent 程序。
- GitHub 不能恢复历史 Task/Snapshot。

如果需要保留历史审批/任务状态，应单独建立加密备份方案。

不要把 Task/Snapshot 直接提交 GitHub。

## 10. Secrets 灾备

至少应在 GitHub 之外安全保存：

1. `/ops/conf/tg-codex-agent/.env`
2. root GitHub SSH Private Key
3. Codex 登录恢复方式

这些 Secret 应使用独立、安全、加密的密码管理器或备份系统保存。

## 11. Fail2ban

仓库根目录的：

`Fill2ban.sh`

属于服务器 SSH 安全基线，不属于 TG-Codex Agent 本体。

它需要独立配置：

- Telegram Bot Token
- Telegram Chat ID
- SSH 白名单 IP

恢复 Agent 时不要求自动执行该脚本。

## 12. 健康检查标准

正式系统至少应满足：

- Agent Python 文件存在。
- Python 编译成功。
- `.env` 存在。
- `codex-runner` 存在。
- Runner Codex CLI 存在。
- Root Codex CLI 存在。
- systemd service enabled。
- systemd service active。
- Git origin 已配置。

执行：

    ./deploy/health-check.sh

只有输出：

    RESULT: HEALTHY

才表示基础恢复检查通过。
