# Phase 2 Step 1：应用接口与版本化契约

## 信任边界与调用方式

正式 Controller 入口为 `Application(Store(project_runtime_dir), allowed_users).handle(request)`。
本模块只接受 Python JSON-compatible dict 并返回 dict；不启动监听、不执行网络请求。
actor 必须由可信 Controller 从已验证事件上下文构造，不得来自消息正文、上传文件、用户资料文本或模型输出。
当前 boundary 校验 private chat、配置白名单及 task ownership；不提供 Controller 身份认证机制，后续 Unix socket transport 必须保障调用者可信。

```python
from tg_testcase import Application, Store

service = Application(Store('data/offline-example'), {'offline-user'})
response = service.handle({
    'protocol_version': 1,
    'request_id': 'offline-create-001',
    'actor': {'user_id': 'offline-user', 'chat_id': 'offline-chat', 'chat_type': 'private'},
    'operation': 'create_or_get_active',
    'payload': {},
})
```

## 请求契约

`protocol.py:validate_request` 是可执行的严格字段验证器。未知字段、未知 operation、无效类型与不支持的版本均拒绝。

| 字段 | v1 约束 |
| --- | --- |
| protocol_version | integer，固定 1（不接受 boolean） |
| request_id | 全局唯一 string，1–120 位安全标识符；首字符字母或数字，后续可为字母、数字、`_`、`.`、`-`；不得含 `..` |
| actor | 仅 user_id/chat_id/chat_type 三个非空 string，每项最多 128 字符 |
| task_id | 除 create_or_get_active 外必填，安全标识符 |
| expected_version | 所有已有 Task 写操作必填，正 integer；创建操作不接受；读操作可省略 |
| operation | 下表中的固定操作名 |
| payload | object，严格按下表字段，不接受任意扩展 metadata |

## 操作

| operation | payload | 行为 |
| --- | --- | --- |
| create_or_get_active | `{}` | 返回 owner 当前 active Task，或原子创建；generated 仍 active |
| append_text | `{"text":"..."}` | 保存 UTF-8 原始文本，登记 pending source |
| append_upload | `{"filename":"a.pdf","data_base64":"..."}` | 严格 base64、安全文件名和类型检查，保存原始字节，不解析 |
| append_notion_reference | `{"reference":"..."}` | 只登记 canonical page ID 或 notion.so / www.notion.so canonical HTTPS page URL |
| finish_collection | `{}` | 冻结当前 batch 的 source IDs 与 finalized_version，返回 pending_processing |
| submit_user_message | `{"text":"..."}` | 持久化 pending 消息，失效旧确认；不推断规则或答案 |
| get_status | `{}` | state、版本、pending 数量、batches、登记 artifact IDs |
| get_summary | `{}` | status 信息加现有 coverage、P0 与不完整来源报告 |
| confirm_generation | `{"text":"生成"}` 或 `{"text":"带缺口生成草稿"}` | 对 READY Task 绑定当前版本，运行现有 gate；不启动生成 |
| cancel | `{}` | 取消任务、失效确认，保留资料与产物；open batch 标为 abandoned |
| reset | `{}` | 显式清空评审设计，保留历史、资料、产物和待处理消息 |
| get_artifact | `{"artifact_id":"..."}` | 只读取当前 task/owner 登记的 output 文件，返回 base64 字节 |

文本/上传上限为 10 MiB；上传类型沿用 Phase 1 的 txt/md/pdf/docx/xlsx/png/jpg/jpeg。
所有新 source 默认 critical，用户不能通过 payload 降低 gate 要求。
Notion reference 不接受任意描述文本、headers、cookie、token 等 metadata；URL 不接受 userinfo、query、fragment 或 credential-like slug。不会访问 Notion，也没有 token 存储字段。

## 响应契约

所有响应包含 `protocol_version`, `request_id`, `ok`, `code`, `task_id`, `version`, `data`, `error`。
成功时 code=ok，task_id/version 为此次操作结果，error=null；data 为上表描述的对象。
失败时 ok=false，task_id/version=null，data={}，error 为 `{"message":"<code>"}`，避免泄露路径或输入文本。
code 集合：ok、invalid_request、forbidden、not_found、request_conflict、stale_version、generation_blocked、storage_error。
`get_artifact` 的 data 额外包含 artifact_id/version/mode/filename/data_base64，不返回服务器路径。
客户端遇到 stale_version 应使用新的 request_id 读取当前状态，再由调用者决定是否重新提交。

## 原子性、去重与版本

SQLite 为权威。单个应用操作在现有进程间 flock 加 SQLite BEGIN IMMEDIATE 下完成。
任务/历史、batch、artifact registry、请求结果与 operation event 在同一事务提交。
request_id 全局去重，规范化整个请求后保存 SHA-256 fingerprint；字段顺序变化不影响 fingerprint，任何内容变化明确 request_conflict。
已通过格式和身份校验的请求结果（包含业务失败）持久化；无效契约/非授权输入不持久化。
完全相同的请求重放返回原响应，不产生第二条操作 event；重启后仍有效。
重放先于 expected_version 检查，因此成功写操作可安全重试。重放结果是历史结果，不能被当作最新 task 状态；需要最新状态应使用新 request_id。
artifact 重放仍重新检查 ownership、登记信息、路径与文件安全，不利用缓存绕过安全检查。
Controller 后续应将可信外部事件 ID 稳定映射为 request_id；events.event_id/request_id 的唯一约束提供一请求一事件的持久化基础。当前没有 Telegram update ingestion。

已有 Task 的每次成功写操作递增 task.version，确认保存确认写入后的版本，复用现有 gate 的版本约束。
资料、pending 消息、reset 及 Phase 1 评审变更失效确认；未处理消息对 formal/draft 都阻塞，open batch 阻塞应用确认。
formal/draft、P0、critical incomplete source 等现有约束不变。生成由现有离线 Engine 执行且必须再次通过 gate，应用没有绕过 gate 的操作。

文件写入使用现有原子持久化流程；数据库失败或进程中断可能留下未登记原始文件，但不会被当作 source/artifact 发布。
SQLite 提交后的 snapshot 失败不撤销事务；调用 Store.recover() 可重建 JSON snapshot。
运行目录必须保持服务私有。artifact 文件读取还通过 dir_fd + O_NOFOLLOW 逐级打开目录与文件，并验证为普通文件。
历史原始资料、产物和请求响应会保留；本步骤没有垃圾清理或 dedupe 过期策略。

## 收集与兼容性

append 只登记来源并加入当前 open batch。finish_collection 冻结集合并进入 REVIEW/待后续 processor 阶段；冻结后新 append 建立新 batch，不改变旧批次。
没有 open batch 时 finish_collection 明确拒绝；原请求重放返回原冻结结果。
当前不解析任何应用来源，包括文本；解析、失败重试、处理完成状态和 review processor 留给后续步骤。
`Engine.add_source` 明确保留为 Phase 1 离线 compatibility path：它继续立即调用 TextParser；不得作为 Telegram 正式入口。
`Engine.add_reference` 同样是旧离线接口，正式 Controller 必须使用应用的严格 Notion reference 验证。
Engine/Store/domain 为可信内部 API，不能直接向不可信 RPC 暴露。

## SQLite migration

原 Phase 1 未版本化 schema 对应 PRAGMA user_version=0；Store 初始化执行迁移到 1。
保留 tasks/history/one_active_user，不重建或删除已有数据库与资料。
新增 requests（fingerprint/结果/actor/operation/时间）、events（请求事件去重/前后版本/时间）、collection_batches（open/finalized/abandoned 与 source IDs/冻结版本）、artifacts（task/owner/相对路径/version/mode）。
迁移以一个事务执行，失败整体回滚；重复初始化跳过已完成迁移；未来未知 schema 版本拒绝打开。
迁移从历史 Task.outputs 补登记 artifact IDs；后续 Phase 1 generation 在同一任务事务自动登记新产物。
旧 Task JSON 没有 pending_messages 时按空数组加载，历史 JSON 保持不变。

## 后续工作

下一步实现离线 collection/review processor：消费冻结 batch、显式维护处理状态、解释待处理消息并提出可审查的规则更改。
随后增加可信 Controller/Unix socket transport、真实上传下载和事件映射；Telegram、Notion 与模型集成不属于本步骤。
应用生成执行/调度、恢复队列、artifact 流式传输、响应大小上限与持久化保留策略均需后续设计。


## Step 2A 兼容扩展

`get_status/get_summary` 的 batches 增加 `processing_status`；Collection 的 `status=finalized` 含义不变。`finish_collection` 仅冻结 batch，离线内部 `Processor` 消费后发布 content/manifest，Task 保持 review。text 与上传来源在登记时分别记录可信 `telegram_text`/`upload` origin 和原始指纹。processing barrier 对 formal/draft 都生效，不能通过 Engine.generate 绕过。详细 schema、迁移和恢复参见 [Step 2A](PHASE2_STEP2A_DELIVERY.md)。本次不增加 transport 或网络接口。
