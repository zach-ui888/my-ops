# Phase 2 Step 2A：离线 Collection/Parser

本阶段提供基础设施和 Telegram text（通过离线 Application 请求提交）、TXT、MD 的确定性解析。不连接 Telegram、Notion、Codex/AI，不生成 Requirement、rule、question、case。项目内未找到 Task 20260929-044208-3427B4 的只读设计文件；实现以此次需求和已存在的 Step 1 代码为依据。

## 调用和状态模型

```python
from tg_testcase import Store, Processor

store = Store('data/offline')  # 仅使用项目内、Git 忽略的私有运行目录
store.recover()               # 启动时、接受请求之前调用
processor = Processor(store)
# batch_id 来自 Application.finish_collection；这是可信内部 API。
# manifest = processor.process(batch_id, worker_id='offline-worker-1')
# manifest, contents = processor.result(batch_id)
```

Collection 状态仍为 `open → finalized`；cancel 将 open batch 标记 abandoned。独立 `processing_status` 为 `pending → claimed → processing → completed / partial / failed`。只有 finalized 且 Task 未取消的 batch 可以 claim。所有来源成功为 completed；部分成功为 partial；全部失败为 failed。未支持的类型产生显式失败 issue，不伪造内容。

`claim(batch_id, worker_id, lease_seconds=60)` 返回 claim 句柄或 None。句柄含 batch_id、worker_id、attempt、fencing_token。`heartbeat(claim, lease_seconds)` 延长有效租约；`parse(claim)` 安全重读并暂存解析结果；`publish(claim, results)` 发布。`process` 是上述步骤的同步便捷组合。租期范围为 (0, 3600] 秒，使用跨进程一致的系统 epoch 时间；部署应保持主机时间稳定。长任务需调用方 heartbeat 或选择足够租期，便捷组合不启动后台续租线程。

claim、heartbeat、processing/staging 更新不修改 Task 内容 version/history。正式发布把整个 batch 的 content、manifest、来源投影、run 终态和一条 Task history 一次提交，Task version 仅 +1，清除 confirmation，状态保持 REVIEW。失败/partial 的正式诊断发布同样只 +1。

## SQLite schema 2

迁移在 `BEGIN IMMEDIATE` 中完成，支持 0→1→2、1→2、重复启动；任一步失败回滚 DDL、数据及 user_version；未来版本在初始化写入前拒绝。原有 tasks/history/requests/events/artifacts 不重写、不删除。collection_batches INSERT 已显式列名。

- collection_batches 新增 processing_status；保留原冻结版本及 source_ids。
- source_inputs：batch/source/task ID、登记 locator、类型、可信 origin、输入 SHA256、byte_length、revision。
- processing_runs：每 batch 一个当前 run，保存 worker_id、attempt、单调递增 fencing_token、lease_until、状态及终态结果 digest。
- processing_staging：当前 token 的内部临时解析结果；不经 result API 暴露。
- sanitized_contents：以 (batch_id, source_id) 唯一登记完整 envelope。
- batch_manifests：每 batch 唯一 manifest。

新 append 操作在同一数据库事务登记输入指纹；finish 冻结该 revision（本阶段 revision=1，不提供替换来源 API）。旧 batch 的来源由迁移登记，旧 finalized batch 默认为 pending。旧材料没有历史 hash/size 时，在第一次安全读取后持久登记指纹，再重新读取并核验；无法证明迁移前材料未被改动。读取失败时 hash/size 可以为 null，结果明确 incomplete，不当作成功。没有可信来源证据使用 `legacy_unknown`，绝不按 text.txt 等文件名猜来源。新 text 的 origin 为 `telegram_text`，上传为 `upload`，Notion 引用为 `notion_reference`。

## Content schema version 1

机器可读定义见 [sanitized-content-v1.schema.json](../schemas/sanitized-content-v1.schema.json)。所有 content 均含：

- `content_schema_version: 1`、`trust: untrusted_source_data`。
- `source_id`、`origin`、`input: {sha256, byte_length, revision}`。
- `parser_version: offline-text/1`、`policy_version: inert-utf8/1`。
- `status: completed|failed`、`completeness: complete|incomplete`。
- `locator: {path, source_id}`、`blocks`、`issues`。

每个 block 是 inert text，具有 trust 标记及 source/path/line_start/line_end/paragraph 定位。CR、LF、CRLF 以物理行定位；连续非空行为一个段落，前置空行 paragraph=0。UTF-8 BOM 从文本去除，但输入 hash/size 针对完整原始 bytes。空白文本、非法 UTF-8、NUL 分别产生 empty_text、invalid_utf8、nul_byte；issues 带 severity 和 locator。单来源限制 10 MiB、20,000 blocks；超限明确失败，不静默截断。协议已拒绝零字节上传/空 text；Parser 自身仍检查空文本，支持 legacy 输入诊断。

MD 与 TXT 均按纯文本处理，不解析执行 HTML，不展开 include，不下载链接或图片。下游显示时仍必须转义 HTML，不能将 sanitized 误解为可安全执行的 HTML 或可信指令。

manifest 包含 batch/task/finalized_version、schema/parser/policy version、run 句柄、终态，以及每个 source 的输入指纹、origin、locator、status/completeness、SQLite content_ref 与规范 JSON content SHA256。规范 JSON 为 UTF-8、ensure_ascii=False、sort_keys=True、separators=(',', ':')。

## 安全和恢复

原始文件路径只取自 SQLite 登记的 source_inputs，并限制为该 Task 的 `source/<单层文件名>`。公共 bounded_read 从项目根目录 descriptor 逐级 O_NOFOLLOW 打开目录，再打开文件、fstat 验证普通文件/单链接/大小限制、有界读取并核验 SHA256 和 byte_length；读取前后校验大小和时间戳，避免仅依赖 inside_project 预检查。拒绝 symlink、目录、FIFO/设备、硬链接、越界及路径替换后不匹配的内容。解析器无网络、shell、环境变量或任意路径访问能力；操作系统级隔离仍由运行环境提供，内部 Python API 和数据库写入权限不能暴露给不可信调用者。

同一 batch 在租约内仅一个有效 claim。过期后重新领取增加 attempt/token 并删除旧暂存；旧 worker 的 heartbeat、parse 暂存提交和 publish 均重新校验 worker/token/租约/取消状态。取消先提交则晚到结果不能发布；发布先提交则取消保留已完成结果。reset 保留 sources、content、manifest。相同 token 的相同终态结果重放直接返回已有 manifest，不重复 history/version/source/result；不同内容的重放拒绝。终态 batch 不自动重试；后续重新解析/版本升级策略留待后续阶段。

Processor 暂存和正式结果全部位于 SQLite，不创建 content/manifest 临时文件。result 只读取已发布登记，崩溃前未提交事务回滚；已提交而快照失败时 SQLite 仍可读取，recover 重建快照。过期 run 在下次 claim 时恢复，不需清空数据库。

上传文件遵循原有“先文件、后登记”机制，数据库登记前不通过公共接口暴露。操作回滚清理未登记上传；进程崩溃留下的生成文件由启动 recover 清理。清理仅针对专用 source 目录中 UUID 命名的未登记文件/临时文件，不删除登记来源、正式解析结果、手工命名文件或输出。Store 根目录必须保持私有。runtime 目录继续由 .gitignore 排除。

Task 增加可缺省的 processing_barrier；旧 JSON 仍可加载。Store 每次读取根据 SQLite 重建 barrier，Engine.generate 的事务内读取同样生效。open/pending/claimed/processing 对 formal 和 draft 都阻塞；终态 critical incomplete 继续阻塞 formal；draft 原有 P0/缺口规则不变。可编辑 JSON 快照不能覆盖数据库门禁。

## 验证和后续范围

自动测试覆盖原有 75 项及 text/TXT/MD、BOM/中文/非法编码/空文本/NUL、hash/size、manifest 定位、并发领取/发布、跨 worker、租约过期、旧 token、取消竞争、幂等、崩溃恢复、事务失败、迁移、生成 barrier、旧 JSON、旧 finalized batch、无 Requirement 写入、安全文件替换和暂存清理。运行命令见 README。

未实现 PDF/DOCX/XLSX/图片 Parser、OCR、真实 Notion/Telegram transport、Codex/AI、业务理解、自动 READY、后台调度服务、RPC 解析结果下载接口。Step 2B 可在本 schema/lease/安全读取边界上扩展文档 Parser、分组件完整性和定位、相应资源限制及恶意样本测试；AI/业务处理与真实集成需另行定义和授权。

本次实际验证：全量 **126 项通过**（原有 75 项 + 新增 51 项）；两处旧迁移测试的目标版本断言由 1 更新为 2。`python3 -m compileall -q src tests examples` 通过；JSON schema 文件语法检查通过。模板 SHA256 保持 `f9b29ba0a28105cd5d8bb00fd749fc03665f65fbc2f050ca7504b7337d69f2a6`。

新增文件：

- src/tg_testcase/content.py
- src/tg_testcase/processor.py
- schemas/sanitized-content-v1.schema.json
- tests/test_processor.py
- docs/PHASE2_STEP2A_DELIVERY.md

修改文件：

- src/tg_testcase/__init__.py
- src/tg_testcase/application.py
- src/tg_testcase/gates.py
- src/tg_testcase/migrations.py
- src/tg_testcase/models.py
- src/tg_testcase/storage.py
- src/tg_testcase/store.py
- tests/test_application.py
- docs/PROTOCOL_V1.md
- README.md

未操作真实材料/凭证，未连接真实外部服务，未修改 tg-codex-agent，未执行 commit/push。工作区未提供可用 Git 仓库元数据（git status 返回 not a git repository），文件清单按本次实际编辑记录列出。
