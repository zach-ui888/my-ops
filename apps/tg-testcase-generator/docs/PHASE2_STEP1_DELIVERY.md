# Phase 2 Step 1 交付记录

已完成 Python 应用边界与 protocol_version=1 契约；无 transport/network integration。

## 文件

新增：
- src/tg_testcase/application.py：应用事务边界、12 项操作、去重、collection、artifact 安全读取。
- src/tg_testcase/protocol.py：严格请求验证、统一响应与错误码。
- src/tg_testcase/migrations.py：事务 migration、artifact registry 登记。
- tests/test_application.py：33 个应用接口测试。
- docs/PROTOCOL_V1.md：契约、调用示例、持久化语义、安全边界和兼容说明。
- docs/PHASE2_STEP1_DELIVERY.md：本交付记录。

修改：
- src/tg_testcase/__init__.py：导出 Application。
- src/tg_testcase/store.py：初始化迁移、任务写入时登记 outputs。
- src/tg_testcase/models.py：向后兼容 pending_messages 字段。
- src/tg_testcase/gates.py：未处理用户消息阻塞生成。
- src/tg_testcase/engine.py：标注旧离线 compatibility path。
- README.md：新增正式入口和文档链接。

## Schema 与操作

PRAGMA user_version 从 0 原地升级为 1；新增 requests、events、collection_batches、artifacts 与唯一约束。
保留 tasks/history/one_active_user，旧 outputs 自动登记，迁移失败回滚，重复执行安全。

协议操作：create_or_get_active、append_text、append_upload、append_notion_reference、finish_collection、submit_user_message、get_status、get_summary、confirm_generation、cancel、reset、get_artifact。

## 验证

执行 `PYTHONPATH=src python3 -m unittest discover -s tests -q`：75/75 PASS。
其中原有 Phase 1 测试 42/42，新应用测试 33/33；tests/test_core.py 未修改。
修改前还单独执行原有 suite，42/42 PASS。
最初尝试 python 命令时环境不存在该别名，改用 python3 后正常执行。

自动测试覆盖：create/replay/冲突、持久化重启去重、并发重复请求、版本冲突、private/白名单/ownership、actor 不从正文推断、延迟解析与冻结 batch、Notion credential-like 拒绝、路径和父目录/文件符号链接、未登记 artifact、跨 owner artifact、确认失效、P0/formal/draft/不完整来源 gate、pending 消息不改规则、cancel/reset/confirm 重放、旧库升级与迁移失败回滚、generated 保持 active、reset 保留产物。

SQLite 仍 authoritative，snapshot 可重建；未修改 Excel/coverage 实现或模板，相关 Phase 1 测试通过。
没有访问真实 Secret、没有网络集成、没有新监听、没有修改 tg-codex-agent，没有执行 Git commit/push。
`git status` 返回 not a git repository，因此无法提供 Git diff 验证；文件范围按实际编辑记录列出。

## 后续

尚无 PDF/DOCX/XLSX/图片/Notion 解析、AI 语义理解、collection/review processor、Unix socket、Telegram/Notion/Codex 网络集成或应用生成调度。
finish_collection 返回 pending_processing；submit_user_message 保持 pending，后续 processor 未完成前不允许生成。
建议下一步先实现离线冻结批次处理与消息 review processor，再实现可信 Controller transport。
文件与 SQLite 不能组成单一事务，中断可能留下未登记文件；这些文件不会通过 source/registry 自动发布。清理、流式 artifact、去重保留期留给后续步骤。
