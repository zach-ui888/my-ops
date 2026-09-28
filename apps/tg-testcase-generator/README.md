# TG 测试用例生成器：V1 第一阶段核心引擎

本阶段交付可离线运行的 Python 核心库，**没有接入 Telegram Controller、生产 Bot 或 Notion**。依据 `docs/V1_REQUIREMENTS.md` 与本次确认的产品规则实现。项目内未提供 Task `20260928-005107-01A988`、`20260928-005507-A46010` 的历史分析原文，未假定已读取这些记录。

## 运行与测试

Linux、Python 3.10+，运行时零第三方依赖；SQLite、文件锁与 XLSX ZIP/XML 操作使用标准库。`pyproject.toml` 提供 setuptools 打包配置（构建工具依赖 setuptools>=61）；源码运行无需安装、联网或配置密钥。

在项目根目录执行：

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
PYTHONPATH=src python3 examples/offline_demo.py
```

示例使用人工编写的合成需求和结构化设计，执行来源保存 → 评审 → 明确确认 → Excel → 覆盖检查 → 取消，并打印产物路径。示例中的“生成”是模拟用户输入，不能在未来的真实 Controller 中自动调用来代替用户确认。示例结束后取消任务以释放名额，资料和产物保留。

测试数据只在项目内 `.test-runtime/` 创建，测试结束删除自身临时夹；这不属于生产 Task 自动清理策略。示例产物在 `data/demo/`。`data/`、`runtime/`、测试运行数据和 `.env` 均已加入 `.gitignore`。部署时只选用项目内这些被忽略的目录作为 Store 根目录，禁止提交 runtime 数据。

## 已实现

- `Engine` 应用入口检查白名单、私聊及 Task 所有者；所有写操作要求当前版本，过期修改失败。
- SQLite 权威状态及全版本历史；部分唯一索引原子约束一个用户只有一个未取消 Task。进程文件锁串行协调 SQLite 与快照写入；事务 `synchronous=FULL`。
- 生命周期：`collecting → review → ready → generating → generated`；生成后可返回评审继续修改。除已取消外均占用活动名额。允许各非终态取消；取消为终态。
- `cancel` 只取消，不删除。`reset` 保留来源，清空当前需求、问题、回答、规则、路径和用例，重新评审；历史评审及旧产物继续保留。已取消 Task 不可 reset，需新建。
- 每次修改递增版本并保存 SQLite 历史。`task.json` 是派生快照；`review/vN.json` 是版本快照。文件通过同目录临时文件、fsync、原子替换和目录 fsync 写入。
- `Store.recover()` 从 SQLite 重建当前快照。中断于 generating 的任务转为 ready、递增版本、清除确认，要求重新确认。应在启动时、接收请求之前调用；不能在正常生成期间当作状态查询调用。
- 来源模型包含 ID、类型、定位、内容、引用、解析状态、完整性、关键性、失败原因和分项完整性。支持 UTF-8 TXT/MD（含 BOM）；空内容、非 UTF-8 或 NUL 标记失败。文档内容仅是数据，不执行其中任何指令。
- PDF/DOCX/XLSX/PNG/JPG/JPEG 可以安全保存原始字节，但解析状态明确为 `unsupported`、完整性为 false。Notion 只登记引用，同样标记 unsupported，绝不访问网络。
- 上传文件类型白名单、10 MiB 限制、严格 ASCII 文件名、路径穿越和符号链接拒绝。调用者传原始字节，核心不接受任意路径去读取上传源文件。
- Requirement、P0/P1 Question、Answer、FinalRule、CriticalPath、TestCase、Evidence 模型；验证整个来源 → 需求 → 最终规则 → 路径 → 用例链。
- 问题用稳定语义 key 和归一化文本去重；已回答的问题不能重复回答。P1 每轮 0～3 个；新一轮仅在有未解决 P0 时允许。显式标注的多来源关键冲突必须是 P0 且关联至少两个来源。P0 不可跳过；回答与最终规则一并保存。已确认规则不能被设计替换静默删除或修改。
- 正式生成：没有未解决 P0、关键来源完整、有实际设计和用例、有当前版本的用户“生成”确认。确认与用户、版本、模式绑定；修改设计/新增来源会使旧确认失效。
- 草稿生成：用户必须明确输入“带缺口生成草稿”。可带 P0 或关键资料缺口生成；文件名及每行用例描述明确标记草稿，覆盖报告不返回 100% 完整覆盖或百分比。模式不能互换。
- 生成前摘要含需求数、路径数、P0 情况、P1 已确认/跳过/待处理情况和来源完整性。
- 反向覆盖检查要求每条路径的每个验收检查都绑定真实用例步骤和预期结果，且业务来源完整；仅存在 ID 不算覆盖。报告覆盖/未覆盖路径、缺少检查、模块/需求/用例数量，以及未建立路径的需求和最终规则。
- AI 路径不进业务路径分母，AI 用例不进覆盖分子。任一来源不完整、P0 未解决、需求/规则缺少路径或草稿模式均禁止整体完整性声明，并隐藏百分比；零业务路径也不报告 100%。未覆盖路径存在时如实报告。

## Excel 模板约束

实际读取 `templates/testcase_template.xlsx`，固定工作表 `测试用例模板`，12 列：用例 ID、模块、描述、前置条件、步骤、预期结果及 G～L 六列执行字段。

Exporter 只替换 worksheet 的数据区域和 dimension；保留原始标题行、工作表名称、列宽、冻结首行、样式、页面属性和其余 ZIP 成员。新行继承第二行和列默认样式。原始模板文件不写入。

第二行起为真实用例，不额外追加模板 Z001。真实用例允许使用 Z001 作为 ID；共享字符串表保留旧字符串不意味着存在多余用例行。G～L 默认空白。所有数据单元格强制 `inlineStr`，包括 `= + - @` 或空白前缀开头的文本，不产生公式节点。非法 XML 控制字符和超长文本拒绝导出，不静默截断。

生成时先校验内存 XLSX，再原子写入并重新读取磁盘文件，验证工作表/12 列、标题、数据/样式、空执行字段、行数、公式安全和模板其他属性。覆盖报告写入同目录 JSON，**不增加追溯列或额外工作表**。

## 持久化与恢复边界

```text
data/<store>/
  tasks.sqlite3             # 权威状态、history 全版本记录
  .lock                     # Linux 进程文件锁
  tasks/<task-id>/
    source/<source-id>-<name>
    review/vN.json
    output/formal-vN.xlsx   # 或 draft-vN.xlsx
    output/*.coverage.json
    task.json              # 可重建快照
```

版本冲突抛出 `VersionConflict`；单用户约束冲突抛出 `sqlite3.IntegrityError`；门禁失败抛出含 reasons 的 `GenerationBlocked`。非法状态和数据抛出 `ValueError`，未授权抛出 `PermissionError`。

SQLite 与文件系统不是同一个事务。上传/导出文件先原子写入，再提交数据库引用；崩溃可能留下未被 SQLite 引用的文件，这些文件保留，不能当作已发布产物。只有 `Task.outputs` 中的记录是成功产物。JSON 快照写入失败不会回滚已提交数据库，错误登记在 `Store.snapshot_errors`，可重试 `recover()`。SQLite 事务失败则不接受该状态版本。

生成异常保留 generating 状态供恢复，不自动重试或假定用户已授权下一次生成。恢复不自动删除旧文件。备份应在停止写入后整体复制 Store 目录及正式模板；恢复时保持来源和输出路径不变，再调用 `recover()`。当前没有数据库跨版本迁移或备份调度器。

路径限制以源码项目根目录为边界，拒绝现有符号链接；运行目录应由服务用户独占，不能允许不可信本地进程并发替换目录。此库不尝试实现对恶意本地文件系统管理员的隔离。当前支持 Linux 本地文件系统，不支持多主机共享存储部署。

## 尚未实现 / 能力边界

- Telegram 命令、上传路由、消息展示、真实身份传递及生产 Bot 接入。
- Notion 认证/读取/子页面/附件解析；PDF、DOCX、XLSX 输入解析，图片 OCR。输入 XLSX 解析与输出模板渲染是两项独立能力。
- AI 自动提取需求、发现冲突、自动编写用例、自然语言答案与问题匹配；当前接受可信应用层传入的结构化设计和问题绑定。
- 语义同义问题自动识别：跨不同措辞必须由未来评审层提供同一稳定 key。当前去重不是 NLP 语义推理。
- 覆盖检查验证来源链和显式验收证据完整性，不判断自然语言步骤与预期结果在业务语义上必然正确；仍需业务评审，不能把结构化检查当成真实执行通过。
- 自然语言修订解析、调度队列、自动重试、生产部署、Excel/LibreOffice GUI 打开验收、真实业务端到端验收。

`Engine` 是未来 Controller 应调用的受控入口，用户身份/聊天类型必须来自可信传输层，不能取自需求文档。Store/domain 模块是内部可信 API，不应直接暴露给外部用户；白名单由调用方配置，本阶段无需任何 Token 或 `.env`。

下一阶段建议先评审领域接口和结构化设计数据，再逐项实现解析适配器及完整性报告；其后在隔离测试环境接入私聊 Controller，并用真实业务资料验收。尚未达到完整 V1 产品验收。

Phase 2 Step 1 的正式 Controller 边界为 `tg_testcase.Application.handle`。
请求/响应 v1、事务去重、收集批次、迁移与离线兼容说明见 [协议文档](docs/PROTOCOL_V1.md)。
`Engine.add_source` 保留 Phase 1 立即解析的离线 compatibility path；正式应用 append 只登记，finish_collection 冻结批次供后续 processor 处理。
