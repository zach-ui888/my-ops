# Phase 2 Step 2C-A — 离线 Notion Source Package

实现范围：无凭据 package 校验、可信 importer 绑定、不可变封存、两阶段冻结、离线正文/附件处理，以及现有完整性 gate 的接入。未实现网络适配、Notion API 请求、credentialed Controller、AI Requirement Review。

本阶段依据用户提供的最终设计。Step 2B 当前已验收基线为 **159/159 OK，0 skipped**；旧交付文档的 139 passed / 20 skipped 是历史执行记录。

## 控制边界与存储

`Application.finish_collection` 冻结 source ID、顺序、revision。它不表示点击时刻的 Notion 事务快照。Notion 内容随后由离线 importer 封存；本阶段没有任何 fetch 实现，`fetching` 只是 acquisition 的逻辑状态。

`OfflineAcquisition` 是受信任的内部 Python 控制接口，不是用户 RPC；调用方必须从自己的 SQLite source registry 取得 claim。用户协议没有 import/claim/任意路径读取入口。package 自带 task/source/revision/root 必须与 registry 匹配；`trusted`、headers、token、cookie、session 等额外字段不能授予权限，均因严格字段白名单被拒绝。普通 URL 只作为文本；含 query 的 HTTP URL 保守拒绝，以排除供应商各异的签名 URL；绝不下载、刷新或展开 URL。

为使封存与 artifact registry 在同一事务内提交，本版将 package canonical JSON 和附件 BLOB 存在 SQLite，而非外部目录。`artifacts/<name>` 是受控相对标签，绝不会被 open。Importer 接收严格的 `artifact_id -> bytes` map，校验 kind/hash/size 后写入；Processor 只能通过 SQLite registry 读取，在 SQL 中限制 BLOB 长度并重新核对 hash/size。无需路径授权或外部文件清理协议。manifest 字段不能指定任意文件、URL、文件读取根目录或可信级别。

`source_packages`、`source_package_artifacts`、`batch_input_manifests` 有禁止 UPDATE/DELETE 的触发器；SQLite 中的 source/revision/package binding 才是控制元数据。封存后的 Task 继续使用这些字节，输入对象或远端随后变化不影响该 revision。

## 迁移与状态机

Schema version 3，单事务支持 0→1→2→3、1→2→3、2→3、重复启动、完整 rollback 和 future version 拒绝。旧 Task JSON 和已发布结果保持兼容，不重写历史处理结果。旧本地输入无 SHA 时，仅在首次处理前做 bounded read 并固定观察值；无法读取时固定 unavailable 标记，随后输出 failed。

新增表：

- `source_fetch_runs`：每个冻结 source 的 attempt、fencing token、租约、acquisition status/outcome。
- `source_packages`：task/batch/source/revision/root 校验后的 package、三类 fingerprint/digest。
- `source_package_artifacts`：附件 registry 与不可变 BLOB。
- `batch_input_manifests`：按冻结顺序的输入身份绑定。

`collection_batches` 新增 `acquisition_status`、`input_manifest_digest`、`inputs_sealed_at`；processing 状态机独立保留。未 claim 的 source 以没有 fetch run 表示 pending。合法路径为 pending→claimed→fetching→sealed，fetching→retry_wait→claimed；重新 claim 增加 attempt/token，旧 worker 不能提交。未封存阶段可以 abandoned，取消 Task 会 abandoned 未封存 runs；已封存 package 保留。租约支持 heartbeat、过期重新 claim。sealed 包含 acquisition outcome completed/partial/failed。

所有 Notion source 封存后，才建立唯一 batch manifest；text/upload 使用登记 SHA256。`batch_input_fingerprint = SHA256(canonical JSON ordered [{source_id, revision, input_fingerprint}])`，Notion input fingerprint 使用 content fingerprint。Processor 在 manifest 建立前无法 claim。

- reference fingerprint：规范化 Notion object ID 的 hash。
- content fingerprint：root、adapter/policy/API 版本、outcome、scope 声明、按顺序的 nodes/gaps，以及 artifact id/kind/hash/size；不含 worker、attempt、时间、package ID、存储 path。
- package digest：完整 canonical JSON 的 SHA256；JSON 键顺序/空白不改变 digest，语义列表顺序保留。

`refresh_notion_source` 用户协议操作接收 `{ "source_id": "..." }`，遵循 owner、expected_version、request replay 校验。已完成处理后显式 refresh 才创建同 source 的新 revision/open batch，清空旧确认与派生 review 数据；还需 finish、离线 import、process。存在 open/pending processing batch 时拒绝 refresh，防止旧 worker 覆盖新 revision。reset 只重置 review 数据，绝不重新 acquisition。旧 revision 的封存内容与处理结果仍可查询。

## Package 与转换约定

严格 schema：`schemas/notion-source-package-v1.schema.json`。运行时额外执行 UTF-8 字节预算、JSON 深度/对象预算、父先子后树结构、根绑定、ID/path 唯一性、hash/size 等不能只靠 JSON Schema 表达的检查。

格式使用离线 adapter 规范化的扁平 `nodes` 树，root 为 page/database/data_source。支持结构性子页、嵌套正文、property、table/table_row.cells、database/data_source 条目正文、附件引用。每个 node 的 parent 必须在前，整包只有一个根。普通 hyperlink/bookmark/embed 的 children 拒绝，避免把链接解释为结构遍历授权。该格式不是原始 Notion API response；未来 2C-B adapter 负责规范化及如实报告分页/权限/展开情况。

`scope` 四个必填布尔值：traversal_complete、pagination_complete、permissions_complete、view_semantics_resolved。缺失字段拒绝；false 一律产生 gap，即使 package outcome 自称 completed。unknown block、relation/synced/reference 未展开或没有展开内容、database/data_source 未声明 expanded、外部/未登记附件、显式 gaps 都产生顶层 partial。无法核实已声明范围时不能用 completed 掩盖。

稳定 locator 包含现有 source_id/path，以及 package_id、source_revision、notion_page_id、notion_block_id、notion_table_id、notion_data_source_id、notion_property_id、column_index、attachment_id。标题/正文/property/cell/文件名/URL 一律 untrusted_source_data。不会创建 Requirement、rule、question 或 TestCase。

附件仅限 PNG/JPG/JPEG/PDF/DOCX/XLSX，调用 Step 2B parser。所有 registry 附件都解析一次，即使 node 遗漏引用；图片无 OCR，保持 partial + visual_unresolved。附件 partial/failed、XML/ZIP/外部关系等缺口向顶层汇总；正文成功不能覆盖。嵌入内容不执行、外部关系不抓取。

## 资源预算与失败处理

首版硬上限：batch 顶层 source 50；单 Notion source 页面 100（含数据库条目页）；遍历深度 16；访问 block 20000；输出 block 20000；每输出文本块 64 KiB；合并正文/附件文本 8 MiB；附件 50 个；每附件 10 MiB；附件累计 100 MiB；单 batch package 合计 256 MiB（canonical JSON + raw artifacts）。

结构限制：package JSON 16 MiB，JSON nesting 24，JSON 值/容器总数 200000，输入和输出 issues 各至多 1000，table row 至多 512 cells。已有 Step 2B ZIP/XML/PDF/图像预算继续生效。对尚未封存的 Notion source 每个预留 4 KiB failure receipt 空间，防止前一个 package 耗尽 batch 后无法报告失败；已完成封存总字节仍不得超过 256 MiB。

非法结构、路径、hash、绑定以及无法安全验证的超限 package 在导入事务前拒绝或整体 rollback，绝不发布截断 package。调用方可在同一有效 fetching claim 上使用 `seal_failure(claim, 'resource_limit')`，由可信 registry 构造小型 failed receipt，不复制被拒绝内容。可保留安全正文的输出超限转为 partial + resource_limit；issues 耗尽保留 issue_limit，不能静默 complete。部分/失败来源默认 critical，formal gate 阻塞；coverage 在 gap/P0/draft 下不得宣称 100%。

网络响应大小、请求次数、timeout、rate limit 均留待 2C-B，没有用离线预算假装实现。

## 使用与验证

完全离线示例（仅合成数据，临时目录位于项目 `.test-runtime`，结束即清理）：

```sh
PYTHONPATH=src .venv/bin/python examples/offline_notion_demo.py
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -q
PYTHONPATH=src .venv/bin/python -X pycache_prefix=.test-runtime/compile-cache -m compileall -q src tests examples
sha256sum templates/testcase_template.xlsx
```

必须使用现有 `.venv`。新增测试直接导入并校验 pypdf 6.1.1、Pillow 11.3.0、defusedxml 0.7.1；最终验收 harness 额外拒绝 skipped 并禁用 socket/subprocess。

最终完整离线验收：**203 tests OK，0 skipped，0 failures，0 errors**（禁用 socket/subprocess 的 harness）。compile 通过；离线示例输出 completed。模板 SHA256 保持 `f9b29ba0a28105cd5d8bb00fd749fc03665f65fbc2f050ca7504b7337d69f2a6`。最后收紧 root ID 后重新运行受影响的 package suite 与 compile。

## 文件清单

新增：

- `src/tg_testcase/acquisition.py`
- `src/tg_testcase/notion_package.py`
- `schemas/notion-source-package-v1.schema.json`
- `tests/test_notion_package.py`
- `examples/offline_notion_demo.py`
- `docs/PHASE2_STEP2C_A_DELIVERY.md`

修改：

- `src/tg_testcase/migrations.py`
- `src/tg_testcase/application.py`
- `src/tg_testcase/protocol.py`
- `src/tg_testcase/processor.py`
- `src/tg_testcase/store.py`
- `schemas/sanitized-content-v1.schema.json`
- `tests/test_application.py`
- `tests/test_processor.py`

Git diff 无法取得：当前环境不被 Git 识别为 working tree。没有查找其他项目或外部 Git 目录，没有 commit/push。默认 examples/__pycache__ 不可写，compile 使用项目内 .test-runtime/compile-cache 成功避开该缓存位置，未改变权限。首次测试命令缺少 `PYTHONPATH=src` 导致 import 失败，已修正；迁移版本断言与新冻结语义的旧测试已同步更新。未访问真实 Notion、网络、secret、其他项目或 TG Codex Agent。
