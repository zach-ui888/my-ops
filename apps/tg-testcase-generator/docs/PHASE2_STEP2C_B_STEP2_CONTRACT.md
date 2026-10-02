# Phase 2 Step 2C-B Step 2.0 Contract Freeze

版本：V1 / 2026-10-02。状态：**Contract Freeze GO；Step 2.1 离线编码 GO；生产接入 NO-GO**。

本规范依据 [canonical design](PHASE2_STEP2C_B_STEP2_DESIGN.md)（审查 Task `20261002-113541-1F50C3`）及当前 `application.py`、`protocol.py`、`receiver.py`、`acquisition.py`、`notion_package.py`、`store.py`、`storage.py` 和 Step 1 交付记录。项目未发现另一份独立审查结论文件；本文吸收 canonical design 内的审查结论及本次要求，不声称读取了不可见的审查记录。

MUST / SHALL 为强制，MUST NOT 为禁止，SHOULD 为建议且偏离须记录原因。本文是后续实现规范，不宣称功能已经存在。本文细化或取代 design 中的建议性选择：V1 SHALL 使用 JSON policy、显式 allow、严格单段 reference、受保护分库 registry 和共用授权提交锁。特别是 design 测试表中的“deny 优先”SHALL 替换为“default deny + explicit allow”；V1 MUST NOT 实现显式 deny rule。

本次 MUST 只新增本文，MUST NOT 修改 Python、schema、Source Package v1、receiver wire v1、图片策略、completeness/formal gate；MUST NOT 联网、访问 Notion/真实 Token、部署或 commit/push。后文生产路径只是配置语义，MUST NOT 在本步骤访问或建立这些路径。

## 0. 公共类型与严格解码

后续表中所有对象字段均必填，除明确标注 nullable 外 MUST NOT 为 null；未列字段 MUST 拒绝。JSON MUST 为 UTF-8、无 BOM、无重复键（每层对象在构建 dict 前检测）、无 NaN/Infinity、无 surrogate。整数 MUST 是 JSON integer，布尔不是整数，MUST NOT 自动把字符串、float 转为整数。输入 MUST NOT trim、大小写折叠或 URL decode，除各节明确允许的 ID canonicalization。

| 类型 | 精确定义 |
|---|---|
| `I63` / `P63` | integer，分别 0..9223372036854775807 / 1..9223372036854775807；溢出拒绝，不回绕 |
| `Name` | ASCII `[a-z][a-z0-9_-]{0,63}` |
| `Identifier` | 现有 `[A-Za-z0-9][A-Za-z0-9_.-]{0,119}`，不含 `..`；仅标识，不是路径 |
| `ID32` | lowercase `[0-9a-f]{32}` |
| `SHA` | lowercase `[0-9a-f]{64}` |
| `UTCms` | `I63`，Unix epoch 毫秒；持久化 deadline/审计时间均用此类型 |
| `Principal` | `telegram:` + 正十进制 user ID，无前导零 |
| `C(x)` | UTF-8 `json.dumps(x, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)` 的字节；无尾换行；不做 Unicode normalization |

本规范配置对象只使用 ASCII 字符串；property raw ID 另见 §8。数组默认保持顺序，声明为 set 的数组 MUST 无重复，canonicalization 前 SHALL 按元素 ASCII 值排序；root 数组按 `(type,id)` 排序。digest MUST 使用 SHA256 lowercase hex；MUST NOT 混用 package digest、content fingerprint、policy digest 和请求摘要。

正常：整数 `1`、ID32 `0123456789abcdef0123456789abcdef`。拒绝：`true` 作为 generation、重复 JSON key、`1.0` 作为 epoch。

## 1. Trusted Identity Contract

### 1.1 可信入口和字段

V1 SHALL 只支持同机受保护 Unix peer 或测试中的不可由请求构造的 injected peer。Gateway MUST 从其认证过的 Telegram update 建立身份；用户正文、转发署名、username、请求内 actor MUST NOT 建立身份。Gateway/Controller 的 IPC 不是 receiver wire v1。

`auth_context` SHALL 恰含以下 9 个字段：

| 字段 | 类型及约束 |
|---|---|
| issuer | `Name`，管理员登记的 gateway 名称 |
| bot_instance | `Name`，管理员登记的逻辑 bot 实例；重启 MUST 保持，换 bot SHALL 新登记 |
| telegram_user_id | integer，1..4503599627370495 |
| chat_id | integer，-4503599627370495..4503599627370495 且非 0；V1 private 进一步要求等于 telegram_user_id |
| chat_type | string enum `private`；其他值拒绝 |
| update_id | integer，0..2147483647；不要求单调连续，不以最大值拒绝乱序更新 |
| issued_at | `UTCms` |
| expires_at | `UTCms`；MUST 等于 issued_at + 60000（TTL 固定 60 秒） |
| nonce | lowercase hex 32 字符，Gateway CSPRNG 128 bits；不能从 update_id 推导 |

管理员 peer 配置 SHALL 为 `(issuer, bot_instance) -> gateway_uid, controller_audience` 的唯一映射；UID 为 0..4294967294 integer，部署服务 UID MUST 非 0，audience 为固定 `notion-controller-v1`。一个 pair MUST NOT 绑定多个 UID；同 UID 可服务多个明确列出的 pair。Controller MUST 从 OS 获取 peer UID，不能接收请求声称的 UID。未知 pair、错误 UID 或错误 audience MUST 拒绝。UID、socket 父目录及 release ownership 同时构成信任边界，nonce 不是认证。

V1 envelope SHALL 恰含 `{audience, auth_context, action, action_digest}`；`action_digest = SHA256(C(action))`，action 是现有 Application request 去除 `actor/request_id` 后的对象，其他字段和现有协议约束不变。Gateway MUST 将用户意图和服务端构造的 task/version 绑定到 action；Controller MUST 重算摘要。MUST NOT 支持未定义签名、远程 HTTP 身份头或把 JSON 自签当可信认证。未来跨主机须另定版本，不在 V1 猜测签名算法。

验证时取 `now_ms`，clock skew 固定 5000ms：新投递仅当 `issued_at <= now_ms+5000` 且 `now_ms < expires_at+5000` 成立才可接受；边界相等按这些不等式处理。检测 wall clock 回退超过 5000ms SHALL 停止新身份接收，待受保护时钟恢复，MUST NOT 清 replay 表。运行等待使用 monotonic clock，MUST NOT 因墙钟回退延长既有 job deadline。

### 1.2 两层幂等与重试

Telegram replay protection SHALL 持久化两个唯一键：`(issuer,bot_instance,nonce)` 和 `(issuer,bot_instance,update_id)`。记录字段为上述身份、`action_digest:SHA`、`request_id:Identifier`、`first_seen_at/retain_until:UTCms`、`state:reserved|completed|rejected`、`response_digest:SHA|null`、`result_ref:Identifier|null`。`retain_until = max(first_seen_at,expires_at)+86400000`；MUST 保留至少至该时刻且未完成 reservation MUST 延长至完成，不能因重启丢失。Gateway 同一 update 重试 MUST 保持同一 nonce、issued_at、expires_at、action 和 actor；MUST NOT 为旧 update 重签新鲜时间。Gateway update ledger SHALL 同样持久保存至少 24 小时，且过期未处理更新 MUST 丢弃而非重新签发。

Controller 验证 peer/结构/身份后，SHALL 先查 replay 表：两个键命中同一记录且身份、时间、摘要完全相同时是 delivery retry；即使上下文现已过期也只能恢复原 reservation 或返回受保护的原结果引用，MUST NOT 创建第二次执行。任一键碰撞但另一键或绑定不同 SHALL `AUTH_REPLAY`。未命中才检查时间并原子插入两键。TTL 过期且记录已 GC 的旧 envelope SHALL `AUTH_INVALID`。重试 lookup MUST 先校验可信 peer；不能用过期上下文读取新内容。

Application request idempotency 是独立层：Controller SHALL 构造 `request_id = 'tg-' + SHA256(C([issuer,bot_instance,update_id]))`（67 ASCII 字符），`actor = {user_id:str(telegram_user_id), chat_id:str(chat_id), chat_type:'private'}`。Application 继续用完整 request fingerprint 与 request_id 去重；同 ID 不同 request SHALL `request_conflict`。MUST NOT 将随机 nonce、source revision 或 expected_version 当 request_id。新 update 表达相同动作产生新 request_id，由业务版本/registry semantic key 再约束，MUST NOT 自动合并不同用户动作。

`task.user_id` MUST 等于 actor.user_id；chat_id MUST NOT 代替 task owner。`expected_version` SHALL 来自 action 中明确绑定的现有 Task version（正整数）；Controller MUST NOT 重试时读取最新版本并替换。create 无 task/version；现有读操作规则不变。Application 负责事务内版本 CAS。source revision 是不同计数器，MUST NOT 填入 expected_version。

replay reservation 在 Application 调用前持久化；崩溃恢复 SHALL 用同 request_id/同规范 request 调用 Application。原请求只有在身份 reservation、owner 和当次动作授权均允许时才可初次 dispatch；已完成回复的重取不得触发 acquire。V1 Controller MUST NOT 通过 Application 缓存读取绕过当前内容读取授权；本规范唯一允许撤销后返回的结果是 §10 的无正文 receipt。

正常：user/chat 均 42，update 7，fresh context 得 actor `{"user_id":"42","chat_id":"42","chat_type":"private"}`；同 update 完整重发返回同请求结果。拒绝：正确 user 但 peer UID 不符；同 nonce 改 reference；用 revision 2 猜 Task version；过期新 envelope。

## 2. Scope Policy Contract

### 2.1 严格 schema（V1 JSON，不接受 YAML）

policy 文件 MUST 为单个 JSON object，总 UTF-8 大小 <=1MiB、嵌套深度 <=16（根为 1）；下面是完整字段集合。map key MUST 唯一，所有 map 数量 <=1024；所有 set 数组长度 1..1024（明确例外见下），MUST 无重复。空 principals/grants/workspaces/credentials map 可表达完全撤销。不存在隐含默认字段。

```text
Policy = {
 schema_version: integer literal 1,
 policy_version: Name,
 policy_epoch: P63,
 default_effect: string literal "deny",
 principals: map<Principal, PrincipalRule>,
 grants: map<Name, Grant>,
 workspaces: map<Name, Workspace>,
 credentials: map<Name, Credential>
}
PrincipalRule = {operations: set<Operation>, grants: set<Name>}
Operation = "append_notion_reference" | "acquire" | "refresh" | "cancel"
Workspace = {verification_ref: Name}
Credential = {workspace_ref: Name, secret_handle: Name,
              generation: P63, enabled: boolean}
Grant = {workspace_ref: Name, credential_ref: Name, roots: array<Root>,
         relations: "deny_follow", synced_references: "deny_follow",
         links: "deny_follow", external_attachments: "deny",
         limits_profile: "notion-standard"}
PageRoot = {type: "page", id: ID32, descendants: "structural"}
DatabaseRoot = {type: "database", id: ID32,
                data_sources: {mode: "explicit", ids: set<ID32>}}
DataSourceRoot = {type: "data_source", id: ID32, expected_database_id: ID32}
Root = PageRoot | DatabaseRoot | DataSourceRoot
```

roots 长度 1..1024，`(type,id)` 唯一，MUST 按 discriminated union 恰好校验对应字段。principal 数值范围同 §1，无 leading zero。`secret_handle` 仅不可解析的元数据标签；本步骤及 Step 2.1 MUST NOT 读取/创建对应 secret。`verification_ref` 指向 registry 中受保护的 workspace 验证记录，字段为 `{verification_ref:Name,workspace_ref:Name,status:verified|revoked,record_version:P63}`；离线测试只使用合成记录，不能宣称真实 workspace 已验证。

`notion-standard` SHALL 固定为现有硬上限：100 pages、depth 16、20000 blocks、单块文本 64KiB、合计文本 8MiB、50 attachments、单附件 10MiB、附件合计 100MiB、JSON 16MiB、batch 256MiB、每未封存 source 4096-byte failure reserve。这里只引用现有约束，不更改 schema 或 gate。调度 timeout/队列不是这个 profile 的隐式授权。

### 2.2 跨引用、授权与无歧义规则

发布 MUST 校验：principal 每个 grant 存在；grant 的 workspace/credential 存在；credential.workspace_ref 等于 grant.workspace_ref；workspace 的 verification_ref 存在、匹配且 verified；credential generation 等于 §3 registry 当前代数，enabled 一致；database data source ID 集合非空，同 grant MUST 存在对应 DataSourceRoot 且 expected_database_id 匹配。每个 DataSourceRoot MUST 有同 grant 的 DatabaseRoot 并被其 ids 列出。MUST NOT 接受 dangling、重复、大小写非规范 ID 或未知 limits_profile。

某 principal 可达的 grants 内，同一个 root ID（不论 root type）MUST 只出现一次；重复即整份 policy 拒绝 `ROOT_AMBIGUOUS`，即使 credential 相同也不取首个。不同 principal 可共享同一个 grant。disabled credential 可被 grant 引用以保留配置，但授权 MUST 拒绝。空 policy MUST deny 所有新获取。

解析顺序 SHALL 是 authenticated principal -> principal.operations -> principal.grants -> 唯一显式 root -> workspace -> root_type -> credential_ref/generation。用户 MUST NOT 指定 grant/workspace/credential 来消歧。root 类型只能由唯一 grant 决定；typed API 返回不同 object/id/parent SHALL 拒绝。后代可以在同 root 内按 structural proof 遍历，但 MUST NOT 因父 root 获准而成为独立入口。page 下结构性 database 仍需同 grant 显式 database/data_source 根和父关系；relation、mention、synced_from、普通链接 MUST NOT 扩权。

| policy operation | 现有/内部动作映射与必需校验 |
|---|---|
| append_notion_reference | Application 同名 operation；严格解析并查显式 root，执行现有 append，尚未获取 |
| acquire | Controller 内部动作；batch finalized 后创建 job/claim，必须有此 operation；非新增 Application wire operation |
| refresh | Application `refresh_notion_source`；owner、expected_version、现有 batch barrier；从受保护 source 取 root，成功后新 revision；真正获取另需 acquire |
| cancel | Application `cancel` 的 task 取消；若 scope 中有该 operation 可显式授权；即使 root/credential 已撤销，已认证 task owner 仍 SHALL 有仅停止其既有 job 的安全取消权限，不能创建读取权限 |

cancel 的 owner 安全停止权限是控制面固定规则，不是显式 deny 的例外。其他 Application operations 不被本 scope policy 隐式授予；保留现有边界，生产 Controller 对内容读还须专门授权。MUST NOT 把 policy.cancel 等同于授予读取。

### 2.3 canonical serialization、digest 和 epoch

实现 SHALL 在完整验证后排序 set 数组、root 数组及 JSON object keys，保留其他数组语义顺序，`policy_digest = SHA256(C(normalized_policy))`（包含 schema_version、policy_version、policy_epoch、credential metadata；不含另存的 digest）。使用相同字节存档并在加载时重算。未知字段、duplicate key、浮点数、YAML alias/merge key 输入 MUST fail-closed；不能先 parse 成 dict 再丢重复项。

首次发布 epoch MUST 为 1。受保护 registry 的 current_policy 行保存 `{epoch:P63,digest:SHA,canonical_json:bytes,published_at:UTCms}`；发布 SHALL 在 §4 锁下以 expected_previous_epoch CAS，下一 epoch 恰为旧值+1。相同规范字节重复发布是 no-op；任何改变（含 generation、enabled、version、grant、verification 撤销）须新 epoch，不允许旧 policy 恢复旧 epoch。epoch 溢出停止发布。没有热加载半份 policy；验证失败不替换旧 policy，但若启动时没有有效 policy MUST 拒绝服务。运行时发现权威记录损坏 SHALL 停止新获取和 commit，不能靠缓存继续。

V1 保守规则：任何新 epoch 生效 SHALL 使所有旧 epoch 未提交 job 失效，即使变更看似无关；MUST NOT 自动重绑旧 job。需要新授权 job（source 尚未 sealed 才允许），历史 job/receipt 保留。

正常最小 policy（verification_ref `verify-a` 和 credential generation 1 的合成元数据先存在）：

```json
{"schema_version":1,"policy_version":"scope-1","policy_epoch":1,"default_effect":"deny","principals":{"telegram:42":{"operations":["acquire","append_notion_reference","cancel","refresh"],"grants":["grant-a"]}},"grants":{"grant-a":{"workspace_ref":"workspace-a","credential_ref":"notion-read-a","roots":[{"type":"page","id":"0123456789abcdef0123456789abcdef","descendants":"structural"}],"relations":"deny_follow","synced_references":"deny_follow","links":"deny_follow","external_attachments":"deny","limits_profile":"notion-standard"}},"workspaces":{"workspace-a":{"verification_ref":"verify-a"}},"credentials":{"notion-read-a":{"workspace_ref":"workspace-a","secret_handle":"notion-read-a","generation":1,"enabled":true}}}
```

拒绝：添加 `deny_rules`、同 key 重复、grant 指向另一个 workspace 的 credential、一个 principal 两个 grant 含同 ID、database 未显式列 data source。

## 3. Credential Generation Contract

受保护 registry SHALL 有 credential_head（PK credential_ref）及不可变 credential_history（PK `(credential_ref,generation)`）。每行字段 SHALL 为 `credential_ref:Name, generation:P63, workspace_ref:Name, secret_handle:Name, enabled:bool, deleted:bool, changed_at:UTCms`；history 追加，head 指向最新。首次创建 generation=1。创建及 policy 引用发布 SHALL 在一个 registry 事务完成，不存在先启用未登记 credential 的窗口。

轮换、enable、disable、workspace/handle 变化及删除 SHALL 每次将 generation 严格 +1，并原子更新 policy_epoch/digest；重复同管理 mutation_id (`Identifier`，唯一，操作摘要 `SHA`) SHALL 返回原结果，不重复递增。disable SHALL enabled=false；delete SHALL enabled=false/deleted=true、保留 tombstone/history，移除 policy 对该 credential 的 grants 及无效 principal 引用，空 principal rule 则移除 principal。被删除的 credential_ref MUST NOT 再使用。enable SHALL 只允许非 deleted 记录并产生新 generation。溢出拒绝。MUST NOT 物理删除使 generation 回到 1。

job 创建 SHALL 绑定当前 credential_ref/generation/workspace/epoch/digest。dispatch、每次新的 API 调用、恢复、最终 seal SHALL 重新验证 enabled=true、deleted=false、generation 精确一致和 policy 精确一致。轮换/disable 后未提交旧 generation job SHALL `CREDENTIAL_STALE`（若同时 epoch 不匹配，按 §7 优先级）；MUST NOT 用新 Token 接着做旧 job。缓存 SHALL 以 workspace/ref/generation/epoch/root 分区并失效；取消在途流，已经观察到的 bytes 不能绕过最终检查。

这里只操作引用、代数和测试元数据。MUST NOT 存储、读取、输出真实 Token；API Worker 将来如何解析受保护 secret 由 Step 2.3 接口和 Step 2.5 管理员部署负责。

正常：generation 1 job 执行中，rotation 发布 generation 2/epoch 2，旧 job 不能 seal；新 job 绑定 2。拒绝：disabled credential 继续 heartbeat 后当作授权、删除后重建 generation 1、更换 handle 不递增。

## 4. Policy Revoke / Commit Ordering Contract

### 4.1 冻结的线性化模型

V1 SHALL 使用同机受保护的两份 SQLite DB：Controller registry DB 与 receiver acquisition DB；不使用跨库原子事务假设，不使用网络授权查询替代提交边界。两方 SHALL 共享一个受保护本地 `authorization.lock`，用进程间排他 `flock`，所有 policy/credential/verification 发布、registry cancel、claim 分配/替换以及最终 seal 必须遵守。锁 MUST 位于本地支持可靠 flock 的文件系统，锁文件/父目录不可被 runner 或 Producer 替换，禁止锁文件 rename/unlink。崩溃释放 OS 锁；MUST NOT 用带超时的普通 lease 替代该锁。

固定锁顺序：`authorization.lock -> registry transaction/read snapshot -> acquisition Store._locked()/BEGIN IMMEDIATE`。实现 MUST NOT 在持有 acquisition lock 时再请求 authorization.lock；MUST NOT 跨网络、输入流读取、下载或 ACK 发送持有这些锁。registry 短写事务可在 acquisition 事务前提交；authorization.lock SHALL 持续持有，保护该阶段权威快照。receiver 对 registry 只读，Controller 是 registry 唯一 writer；两服务可访问锁，Producer/runner 不可访问。该拓扑是 V1 的隔离边界，不是独立主机服务。

revoke 的线性化点是持有 authorization.lock 下 registry policy/credential/cancel 更新事务的 COMMIT；更新须原子发布 epoch/head/审计事件。seal 的线性化点是仍持有同一锁时 acquisition SQLite COMMIT。revoke 先提交，则 seal 必须看到新状态而回滚；seal 先提交，则 revoke 不删除历史 package。所谓“撤销已成功”MUST 仅在 revoke COMMIT 后报告，排队等待锁不算完成。不能承诺撤销早于已经进入原子提交的 seal。

### 4.2 Step 2.1 和 Step 2.2 的明确分工

Step 2.1 SHALL 实现 shared guard/registry policy snapshot 与 `authorize_commit(job, claim, package_id, package_digest, now_ms)` 的离线接口和并发契约测试，返回不可由输入伪造、只在 guard 持有期有效的授权决定。检查项：当前 epoch/digest、principal/grant/root/workspace 映射、credential generation/enabled、job 状态/cancel/deadline、claim attempt/fencing/worker、package pin。Step 2.1 MUST NOT 宣称仅调用此接口即可保护当前 acquisition。

Step 2.2 SHALL 将该检查实际接入 `OfflineAcquisition._commit_staged` / `ReceiverAcquisition._commit_staged` 的最终提交路径：完成全部 staging/COMMIT EOF 后取 guard，查 registry，进入 acquisition 事务，复核当前 task owner/cancel/source revision/claim/lease/root/budget，执行授权检查并在 package/artifacts/manifest/outbox 插入后、COMMIT 前再核验 deadline 和 claim lease。整个过程中 MUST 持有 guard，出错回滚；MUST NOT 只在 `receive()` wrapper 或 HELLO 提前检查。所有生产 seal（含 failed package）和 replay 分支 MUST 使用受保护路径，生产 MUST NOT 暴露无 guard 的旧 importer API；离线 fixture 可以继续测试原 API，但不能成为生产旁路。

deadline/lease 在最终检查瞬间的有效性决定能否提交；一旦 SQLite COMMIT 开始不得声称可中断到更精确的墙钟边界。Step 2.5 SHALL 测量此窗口并使租约预算覆盖实际事务。取消同 revoke 使用 guard；现有 Application task cancel 在 acquisition DB 事务内串行化，并由 Controller 在进入该操作前持 guard，成功后传播 registry job cancel。崩溃丢失传播时，最终 task.cancelled 检查仍 MUST 阻止 seal。

已提交 package 的历史 receipt 查询不需要当前 root 读取 grant，见 §10；但不得返回正文/artifact，不能作为新 acquire/refresh 的权限。远端 Notion 多次请求不形成事务快照，MUST 只声明“获取期间观察到的内容”。

正常竞态：seal 持 guard -> DB commit -> revoke 获 guard -> epoch+1，receipt 仍 committed。拒绝竞态：wrapper 授权后 revoke 提交，最终 seal 仅查旧缓存；Step 2.2 测试 SHALL 证明此路径被拒绝。

## 5. Protected Job Registry Contract

### 5.1 持久化与记录

registry SHALL 在受保护本地 SQLite 中持久化，开启 foreign_keys、synchronous=FULL；registry 与 acquisition DB 均 MUST 不可由 runner/Producer 写。Step 2.1 仅在项目内测试目录创建合成 DB；不能将开发 checkout DB 称为生产权威。请求不能直接提交 registry row 或路径。registry schema 是后续内部新 schema，不修改现有 Source Package schema。

| 记录/字段 | 类型、唯一键与语义 |
|---|---|
| jobs.job_id | `Identifier`，Controller 生成 UUID4 hex，PK |
| principal / request_id / intent_digest | `Principal` / `Identifier` / `SHA`；来自可信 reservation |
| semantic_key | `SHA`，UNIQUE，算法见下 |
| task_id / batch_id / source_id | `Identifier`；引用 acquisition 中已存在输入，不能由 HELLO 创建 |
| task_expected_version | `P63`；用户动作接受时的 CAS 输入，仅审计，不是长期 seal 版本条件 |
| revision | `P63`；当前 source revision，seal 必须精确一致 |
| workspace_ref / grant_ref / credential_ref | `Name`，创建后不可变 |
| policy_epoch / credential_generation | `P63`，创建后不可变 |
| policy_digest | `SHA`，创建后不可变 |
| root_type / root_id / canonical_root_id | enum page/database/data_source；`ID32` / `ID32`；root_id 是 policy 登记根 ID，canonical_root_id 是解析后供 wire/package 比对 ID，V1 两者 MUST 相等，不能把内部 grant key 当 root_id |
| state | 下述封闭枚举 |
| attempt / fencing_token | `I63`；未 claim 为 0，claim 后 >0，拷贝 acquisition 权威值，不另起计数 |
| worker_id | `Identifier|null`；Controller 分配，非 Producer 自报 |
| created_at / updated_at / deadline | `UTCms`；deadline 创建后不能延长；V1 `created_at+900000`，这是契约时限而非 RSS 验收指标 |
| lease_until / retry_at | `UTCms|null`；未 claim/未 retry 为 null；acquisition 秒值转换时 floor(seconds*1000)，安全向下取整 |
| cancel_state / cancel_at | `none|requested|effective` / `UTCms|null`；requested 即禁止新操作；effective 表示停流完成，不等待它才阻止 commit |
| package_id / package_digest / package_length | `Identifier|null` / `SHA|null` / integer 0..16777216 或 null；作为一组原子 pin，只有 package 已构造校验后设置，之后不可变 |
| spool_state / spool_ref | `none|pinned|retained|removed` / `Identifier|null`；ref 为注册标签，不接受任意路径 |
| receipt_state / receipt | `unknown|committed|not_committed|conflict` / null 或恰含 `{package_id:Identifier,package_digest:SHA,content_fingerprint:SHA}` |
| last_error / failure_count | §7 enum 或 null / `I63`；MUST NOT 存原始异常/响应 |

SHALL 另建 job_attempts，PK `(job_id,attempt)`，保存 `fencing_token:P63,worker_id:Identifier,lease_until:UTCms,state:claimed|fetching|retry_wait|sealed|abandoned|lost,started_at:UTCms,ended_at:UTCms|null`；每个 `(batch_id,source_id)` 同时最多一个活动 job/claim（partial unique constraint + guard），不能用 policy 变化制造并行写入。claim `revision` 必须同 job。

`semantic_key = SHA256(C(["job-v1",principal,task_id,batch_id,source_id,revision,workspace_ref,grant_ref,root_type,canonical_root_id,policy_epoch,policy_digest,credential_ref,credential_generation,"acquire"]))`。intent_digest 为本次规范 action 摘要；创建重放同 semantic_key 且绑定一致 SHALL 返回原 job（即使来自另一 delivery），不重复 claim；同 request_id/动作却变更绑定 SHALL conflict，不静默新 job。不同 intent/request 可指向同 semantic job，SHALL 在独立 request_jobs 表记录 `(request_id,job_id)` 唯一映射；同 request_id MUST NOT 映射多个 job。新 epoch 可新建 job，但必须先终结旧活动 job，且未 sealed；已 sealed 必须显式 refresh/new revision。

任务其他合法编辑会递增 task version，MUST NOT 因此让已绑定 source 的 job 自动失效；seal 检查 task owner/cancel、source revision、batch 状态及授权。refresh MUST 由现有 Application 创建新 revision/new batch，registry MUST NOT 自增 revision。原 reference 可以是 UUID/URL；进入 Application 前 SHALL 转 ID32 存储，历史 locator 需严格 parser 成功才可入 registry。

### 5.2 完整状态机和 claim 顺序

状态枚举：`registered, claim_pending, claimed, fetching, retry_wait, prepared, submitting, reconciling, committed, cancelled, revoked, expired, failed`。以下为所有允许边；未列边 MUST 拒绝。terminal 为 committed/cancelled/revoked/expired/failed，terminal 只允许同状态幂等更新审计；历史提交证明只能在 reconciling 分流前确认，不能先宣布 cancelled 再改 committed。

| 起点 | 允许终点 / 条件 |
|---|---|
| registered | claim_pending；或 cancelled/revoked/expired/failed（没有提交可能） |
| claim_pending | claimed（claim 绑定落库）；reconciling（创建 claim 中断）；或 cancelled/revoked/expired/failed（确认无提交） |
| claimed | fetching；retry_wait（启动暂时失败）；reconciling（丢 lease/崩溃）；或 cancelled/revoked/expired/failed（未发送） |
| fetching | prepared（spool pin 完成）；retry_wait（暂时失败）；reconciling（崩溃/claim 不确定）；或 cancelled/revoked/expired/failed |
| retry_wait | claim_pending（旧 claim 过期/释放后新 attempt）；reconciling（claim 状态不确定）；或 cancelled/revoked/expired/failed |
| prepared | submitting；reconciling（崩溃）；或 cancelled/revoked/expired/failed（尚未发送） |
| submitting | committed（验证 ACK）；reconciling（任何不确定结果，包括 cancel/revoke/timeout/ERROR）；MUST NOT 直接标 not_committed |
| reconciling | committed（权威 receipt）；submitting（同 pin、同有效 sealed/fetching claim 且仍授权）；retry_wait（确证无 commit、可重试且仍授权）；cancelled/revoked/expired/failed（确证无 commit 后按原因终止） |

cancel/revoke/timeout 在 submitting/reconciling 时先记录阻断原因，MUST 停止新发送且转/保持 reconciling，直到 §10 确认是否已提交。数据库不可用时不得猜成失败。确定无提交后的终止原因优先 cancel -> revoked -> expired -> failed；错误分类按 §7，状态优先级与诊断优先级不混用。

claim 顺序 SHALL 固定：持 guard 验证授权及 finalized source -> registry 提交 registered/claim_pending intent（worker_id 固定）-> acquisition.claim -> registry 记录返回 attempt/fence/lease 并转 claimed -> acquisition.transition(fetching) -> registry fetching -> 才允许 worker 获取或连接 receiver。claim 返回 None 不派发；转 reconciling 查询原因。崩溃在 claim 创建后、registry 落库前 SHALL 按 `(batch,source,revision,worker_id)` 查权威 claim：一致则补全，未知或冲突不接管；等待旧 lease 失效再由 acquisition 分配更大 attempt/fence。MUST NOT 按 HELLO 构造 claim。新 attempt 旧 worker 永久 fenced。

heartbeat 只针对 claimed/fetching acquisition 状态，独立于下载读写；prepared/submitting 时 acquisition 仍 fetching，直到 commit。默认 claim lease 60 秒、heartbeat 每 20 秒，延长不超过 job deadline；这些是 V1 离线默认值，生产必须通过 §11 测量验收，不能据此宣称性能合格。retry_wait 必须将 acquisition 转 retry_wait 或等待其租约失效；未到 retry_at 不 claim。网络等待不持 DB 写锁。

pin 后 retry MUST 使用同 package_id/digest/canonical bytes/artifact bytes。若需要重新 fetch，必须未 pin 且新 attempt；已 pin spool 丢失 SHALL failed（先对账排除 commit），MUST NOT 用相同 job/package ID 重建不同内容。重试新 attempt 可使用原 pin，仅 HELLO 的 attempt/fence 更新为权威 claim。package_id 全局唯一；source `(batch_id,source_id,revision)` 至多一份 package；同 digest 不同绑定、同 package_id 不同 digest 均 `DIGEST_CONFLICT`。

### 5.3 分库 outbox / recovery

Step 2.2 SHALL 在 acquisition seal 的同一事务追加 acquisition_commit_outbox，PK event_id（`Identifier`），UNIQUE job_id、UNIQUE `(batch_id,source_id,revision)`；字段 SHALL 为 `job_id,task_id,batch_id,source_id,revision,attempt,fencing_token,policy_epoch,policy_digest,credential_ref,credential_generation,package_id,package_digest,content_fingerprint,committed_at`，类型同 jobs。该记录是内部元数据，MUST NOT 进入 package/wire。source_packages、artifacts、sealed claim、batch manifest 和 outbox SHALL 一起 commit/rollback。

reconciler 只读 acquisition/outbox，在 registry 单独事务幂等写 receipt/committed 和 `consumed_events(event_id PK, event_digest SHA)`；不在 acquisition 标记“已发送”来制造第二个原子性要求。event_id 重复内容不同为 conflict。崩溃可无限次重读未消费事件，不能重新获取内容。outbox SHALL 至少与 source package 同期保存；V1 无自动 package/outbox/receipt 删除。registry 暂时更新失败不影响已提交事实，状态保持 reconciling。

Step 2.1 SHALL 实现 outbox DTO、消费幂等性及 fake acquisition port；Step 2.2 才修改 acquisition migration/commit 接线，不要求 Step 2.1 实现 socket。独立 DB 不采用 ATTACH 假装跨库可靠提交。若 outbox 缺失但 package 存在，仅可通过全部已 pin 的 task/batch/source/revision/package_id/digest 比对确认历史；记录 integrity audit、阻止新获取直至修复，不能合成未知 job 授权。

正常：claim 后崩溃，恢复匹配 worker/attempt 后继续；seal 后 registry 更新前崩溃，从 outbox 得同 receipt。拒绝：Producer 自报 job 创建 claim、旧 fencing 覆盖新 attempt、同 semantic key 改 digest。

## 6. Strict Notion Reference Grammar

安全入口 SHALL 新增纯离线 `parse_notion_reference(text) -> ID32`，按原始字符串严格 fullmatch，不使用 permissive URL parser 再清理。ASCII grammar：

```text
HEX       = [0-9A-Fa-f]
ID        = HEX{32}
UUID      = HEX{8} "-" HEX{4} "-" HEX{4} "-" HEX{4} "-" HEX{12}
ATOM      = [A-Za-z0-9]+
SLUG      = ATOM ("-" ATOM)*       ; 1..80 ASCII characters total
ROOTID    = ID | UUID
SEGMENT   = ROOTID | SLUG "-" ROOTID
HOST      = "notion.so" | "www.notion.so"
URL       = "https://" HOST "/" SEGMENT
REFERENCE = ROOTID | URL
```

输入总长度 1..512 ASCII bytes；唯一 segment 长度 1..117（80+1+36），不得尾斜杠、空 segment、多个 segment。解析 SEGMENT SHALL 先尝试整个 ROOTID，再尝试末尾 `-UUID`，再末尾 `-ID`，剩余 prefix 必须完整满足 SLUG；不接受无分隔 slug+ID。URL scheme/host 只接受上述 lowercase 字面量；hex 大小写均接受且最后去 UUID 标准连字符、转 lowercase。MUST NOT 限定 UUID version/variant 位（只验证格式）。

MUST 预先拒绝任何 `%`（因此拒绝一切 percent encoding、encoded slash/backslash、double encoding）、`\\`、非 ASCII、C0 0x00..0x1f、DEL 0x7f、空白、`?`、`#`，包括尾随空 `?/#`。grammar 自动拒绝 userinfo、port（含 :443）、userinfo 编码、Unicode/punycode host、host 尾点、notion.site/custom domain、`/workspace/id`、`/id/view`、query view URL。不支持任何以 view ID/数据库 query 表达过滤视图的形式；无 query 的 root ID 无法仅凭字符串判定是 view，SHALL 只按显式登记 root 精确匹配，未登记 ID 不探测。URL 仅提取 ID，MUST NOT fetch、DNS resolve 或 follow redirect。

兼容策略：Step 2.1 新安全入口 MUST 先严格解析，再把 ID32 写入现有 Application `payload.reference` 并调用未改动 `protocol.validate_request`。现有 `reference_id()` 仅对已可信验证值做旧规范化，MUST NOT 作为 parser/授权。生产 Controller MUST 禁止绕过新入口直达旧宽松校验。历史 reference 要重新走严格入口，失败就要求新动作，不自动“修好”非标准连字符。Step 2.1 不修改原协议函数的兼容行为。

| 输入 | 结果 |
|---|---|
| `0123456789ABCDEF0123456789abcdef` | `0123456789abcdef0123456789abcdef` |
| `01234567-89ab-cdef-0123-456789abcdef` | 同上 |
| `https://www.notion.so/My-Page-0123456789abcdef0123456789abcdef` | 同上 |
| `https://notion.so/01234567-89ab-cdef-0123-456789abcdef` | 同上 |
| `0123-456789abcdef0123456789abcdef` | 拒绝非标准 UUID |
| `https://notion.so/x0123456789abcdef0123456789abcdef` | 拒绝无 slug 分隔符 |
| `https://notion.so/0123456789abcdef0123456789abcdef?` | 拒绝空 query；尾随 `#` 同理 |
| `https://u@notion.so:443/0123456789abcdef0123456789abcdef` | 拒绝 userinfo/port |
| `https://notion.so/a%252Fb-0123456789abcdef0123456789abcdef` | 拒绝 double encoding |
| `https://notion.site/0123456789abcdef0123456789abcdef` | 拒绝非白名单 host |

边界用例 SHALL 覆盖 80/81 字符 slug、117/118 segment、512/513 总长、额外 segment、CR/LF/tab/NUL、反斜杠、`%2f/%5c/%25`、`?v=`；不能因为先 urlsplit 丢空 delimiter/控制字符而接受。

## 7. Internal Error Contract

V1 内部错误为下表封闭 enum。`retryable=true` 只表示同一已授权 job 在 deadline/预算内可重试，不得绕过失效授权；SHALL 先 §10 对账再决定是否重发。未知异常统一 INTERNAL_FAILURE，不回显 exception。多个错误同时存在时 SHALL 按表中优先级数值升序选择第一项（同数值按表内顺序）；只检查已具备前置可信数据的条件，不能为了更高优先级泄露资源存在性。

Application mapping 是 Controller 使用现有 response.code 的映射，不扩展协议 enum；受保护 job status 可以返回内部 enum，但用户普通响应不能输出内部路径/credential。所有 receiver wire v1 错误 MUST 固定 `{"protocol_version":1,"type":"ERROR","code":"seal_rejected"}`。

| 优先级 | enum | 触发条件 | retryable | audit 分类 | Application code |
|---|---|---|---|---|---|
| 10 | AUTH_INVALID | peer/pair/audience/identity 类型/时间/摘要不合法 | false | identity | forbidden |
| 11 | AUTH_REPLAY | nonce/update 唯一键不同绑定碰撞 | false | identity | forbidden |
| 12 | OWNER_MISMATCH | trusted principal 非 task owner | false | identity | forbidden |
| 20 | POLICY_INVALID | schema/重复键/跨引用/摘要损坏/epoch 发布 CAS 失败 | false | configuration | forbidden |
| 21 | ROOT_AMBIGUOUS | principal 多重 root 匹配 | false | configuration | forbidden |
| 22 | CREDENTIAL_STALE | generation 不同、disabled/deleted | false | authorization | forbidden |
| 23 | POLICY_REVOKED | job epoch/digest 非当前或 verification 撤销 | false | authorization | forbidden |
| 24 | SCOPE_DENIED | principal/operation/显式 root 未获准 | false | authorization | forbidden |
| 25 | WORKSPACE_MISMATCH | workspace 绑定/proof 不一致 | false | authorization | forbidden |
| 30 | JOB_CANCELLED | task cancelled 或 registry requested/effective | false | lifecycle | invalid_request |
| 31 | JOB_TIMEOUT | now >= deadline | false | lifecycle | invalid_request |
| 32 | FENCED | worker/attempt/fence 被替换或不匹配 | false | lifecycle | invalid_request |
| 33 | LEASE_LOST | lease 到期/claim 不存在，且不是历史 receipt 查询 | false | lifecycle | invalid_request |
| 40 | REFERENCE_INVALID | §6 不匹配 | false | input | invalid_request |
| 41 | ROOT_TYPE_MISMATCH | object/id/type/parent 不符 policy | false | scope | invalid_request |
| 42 | VERSION_CONFLICT | Application expected_version CAS 失败 | false | concurrency | stale_version |
| 43 | REQUEST_CONFLICT | request_id 或 reservation 绑定不同 request | false | integrity | request_conflict |
| 44 | DIGEST_CONFLICT | pin/package/source/receipt digest 冲突 | false | integrity | request_conflict |
| 45 | PACKAGE_INVALID | schema/frame/order/hash/artifact/EOF 不合法 | false | integrity | invalid_request |
| 46 | PROPERTY_ID_INVALID | raw property ID 类型/长度/Unicode 不合法 | false | adapter | invalid_request |
| 47 | LOCATOR_COLLISION | 同 locator 不同 raw tuple | false | integrity | invalid_request |
| 50 | NOTION_AUTH_FAILED | 固定 API 返回 401；隔离 credential 等管理处理 | false | upstream_auth | forbidden |
| 51 | NOTION_RESOURCE_UNAVAILABLE | 403/404；不得区分存在性 | false | upstream_scope | not_found |
| 52 | PAGINATION_INVALID | cursor 循环/类型或分页一致性失败 | false | upstream_data | invalid_request |
| 53 | VIEW_UNRESOLVED | 需要不支持的 view 语义 | false | scope | invalid_request |
| 54 | RESOURCE_LIMIT | 任意既有硬预算超限 | false | budget | invalid_request |
| 55 | ATTACHMENT_SSRF_BLOCKED | 地址/host/路径/IP 策略失败 | false | egress | invalid_request |
| 56 | ATTACHMENT_REDIRECT_REJECTED | 下载发生 redirect | false | egress | invalid_request |
| 57 | ATTACHMENT_EXPIRED | 获准附件短期能力过期 | true | attachment | invalid_request |
| 60 | RATE_LIMIT_EXHAUSTED | 429/529 等待/重试预算已耗尽 | false | upstream_capacity | invalid_request |
| 61 | UPSTREAM_UNAVAILABLE | 可重试网络失败/5xx，仍有预算 | true | upstream_capacity | storage_error |
| 62 | STORAGE_FAILURE | SQLite/磁盘/锁暂时不可用；提交事实未知须对账 | true | local_storage | storage_error |
| 63 | RECEIPT_PENDING | DB 不可用/提交状态无法确定 | true | reconciliation | storage_error |
| 64 | RECEIPT_NOT_FOUND | 受保护完整 lookup 确证无 package | false | reconciliation | not_found |
| 90 | INTERNAL_FAILURE | 未分类内部异常/不变量损坏 | false | internal | storage_error |

rate limit 的等待本身不是 EXHAUSTED；耗尽后不能以 retryable=true 无限重排。ATTACHMENT_EXPIRED 只允许 Step 2.4 通过 Step 2.3 对同获准对象有界更新元数据；非用户指定 URL。

内部错误 MUST NOT 塞入 package.gaps。Step 2.3/2.4 的内容缺口映射 SHALL 只使用既有 enum：分页不完整 -> pagination_incomplete；权限缺失 -> permission_missing；不支持 view -> view_semantics_unresolved；预算 -> resource_limit；拒绝 external 附件 -> external_attachment_rejected；relation/synced -> 各自 unresolved。授权、身份、revoke、digest conflict MUST 中止提交，不生成“已授权失败 package”规避授权。其余失败若无准确现有 gap，可在仍授权时构造现有 failed outcome（现有 acquisition_failed 机制），MUST NOT 创造新 gap。图片仍为 raw PNG/JPEG + descriptor，visual_unresolved 与所有 gate 不变。

审计 SHALL 只存错误 enum、匿名 principal（受保护映射或不可逆服务内别名）、job/request ID、policy epoch/digest、时间、计数、endpoint 模板；MUST NOT 存 Token、正文、raw URL/property ID、headers、原始异常。正常：暂时 DB busy -> STORAGE_FAILURE -> reconcile；拒绝：把 AUTH_INVALID 放入 gaps 或将详细错误写入 receiver ERROR。

## 8. Property ID Locator Contract

raw Notion property ID 不保证 UUID。Step 2.3 adapter SHALL 将 JSON 解码后的 raw ID 作为不透明 Unicode string：1..1024 UTF-8 bytes，拒绝 NUL、C0、DEL、surrogate；MUST NOT URL-decode、lowercase、去连字符或 Unicode normalize。`%3A` 与 `:` 是不同 raw ID。HTTP endpoint 的路径编码由 typed client 独立完成，不能复用 locator 或对 raw ID 二次解码。

安全 property node locator SHALL 定义为：

```text
owner = canonical ID32 of the page/database/data_source carrying the property
payload = C(["notion-property-v1", workspace_ref, owner_type, owner, raw_property_id])
locator = "np1-" + SHA256(payload)
```

owner_type 为 page/database/data_source；workspace_ref 为 `Name`。输出固定 68 ASCII 字符，满足现有 identifier()。adapter SHALL 在受保护 job scope proof 中维护 `locator -> payload bytes` 的唯一登记，已有 locator 仅在 payload 完全一致时复用；不同 payload 同 hash SHALL `LOCATOR_COLLISION`，停止构建，MUST NOT 用依赖遍历顺序的 `-2` 后缀或静默合并。有限长度 hash 不可能数学上保证所有输入绝无碰撞；这里的“无碰撞”承诺是**每个被接受 package 内不会把不同 raw tuple 合并为一个 locator**，由精确比较和 fail-closed 保证，不能声称 SHA256 是无条件单射。

property 分页项需要多个 node 时 SHALL 使用 `npi1-` + SHA256(C(["notion-property-item-v1", locator, ordinal]))，ordinal 为 0..19999 的 integer，按 API 观察到的逻辑顺序连续编号且受现有 20000 blocks 总预算约束；同 tuple 在一次 job 内确定，跨观察变化不保证 ordinal 指向同内容。同样执行碰撞登记；不得依赖随机数/cursor/Token/获取时间作为 locator 输入。

Step 2.3 负责 raw ID 验证、算法、collision detection、合成 hash collision 测试及 adapter DTO（安全 locator +规范化内容）；Step 2.4 builder 只使用该安全 locator，MUST NOT 把 raw ID 直接传 identifier()，MUST NOT 将 raw-to-safe 表加入 package。相同 workspace/owner/raw tuple 跨重试和不同 policy epoch SHALL 相同输出；不同 owner 有不同 namespace 输入。Step 2.4 SHALL 再检查 package node 全局 ID 唯一性（其他 node namespace MUST NOT 使用 np1-/npi1- 前缀）。

正常：raw `f%3AAb` 在 owner A 得稳定 np1- locator，传 identifier() 的只是 locator。拒绝：直接用 `f:Ab` 作 node id；raw 超 1024 bytes；测试 hash 强制碰撞却覆盖已有记录。

## 9. Production Path Contract

Step 2.1 SHALL 定义不可由用户 action/HELLO 提供的 `RuntimePaths` strict object：

| 字段 | 用途/owner |
|---|---|
| release_root | 受保护代码/依赖；管理员 owner，服务/runner 不可写 |
| config_root | policy/API/limits/peer 配置；管理员 owner，服务按需只读 |
| registry_state_root | registry DB、replay ledger；Controller owner |
| acquisition_state_root | acquisition DB、sidecars、Task 快照；Receiver owner，受保护 Controller 只通过授权组件访问 |
| receiver_staging_root | Receiver owner，0700，显式注入 Staging |
| producer_spool_root | API/Producer 专用 owner，0700 |
| downloader_staging_root | Downloader 专用 owner，0700 |
| socket_root | 服务 owner，0750；具体 socket 0660、专用 group |
| authorization_lock_path | Controller 预建文件，0660，共用 guard 专用 group；父目录不可由 Producer/runner 写 |

每项 SHALL 是长度 1..4096 bytes 的绝对本地路径，不含 NUL、`.`/`..` segment、symlink；UNIX socket 派生完整路径 encoded bytes SHALL <=107。state/staging/spool SHALL 两两不重叠且不位于 release/config 内；允许 authorization.lock 位于单独共享受保护目录，不能放在 Producer spool。socket basename 固定 `gateway.sock` / `receiver.sock`，不得由 job 拼接。state DB 文件名固定 `registry.sqlite3` / `tasks.sqlite3`，spool_ref 只经受保护登记映射路径。测试 SHALL 注入项目内临时目录及 fake ownership，不使用真实生产路径。

`RuntimeOwners` SHALL 恰含 `administrator_uid, gateway_uid, controller_uid, receiver_uid, producer_uid, downloader_uid, guard_gid, gateway_gid, receiver_gid`（0..4294967294 integer）；服务 UID SHALL 非 0、互相独立、无登录用途。所有路径及父目录 MUST 无 runner 写权限，runner 不得在上述组；配置/代码不从工作目录/PYTHONPATH/用户 site 加载。controller 对 acquisition 的现有 Application 写操作 SHALL 在受保护安装内执行，MUST NOT 授予任意 SQL 或让 Gateway 直接写 DB。

Step 2.1 只定义配置类型、路径验证接口及 fake filesystem 测试，不迁移生产。Step 2.2 SHALL 解除 Store/storage 对 PROJECT 的硬耦合，改为显式 capability roots/descriptor-relative/no-follow 的允许根，不是删除所有路径检查；同时将 receiver_staging_root 注入 `receive()` 内实际 importer。生产模式缺配置 MUST fail-closed，不能 fallback 到 checkout/store.root。Step 2.4 实现 spool/downloader 路径生命周期；Step 2.5 提供保护权限、安装与迁移验收模板，但不自动部署或访问 credential。

正常：测试注入项目内分离的 registry/acquisition/staging 目录；生产配置由管理员登记且 ownership 验证通过后才能启用。拒绝：用户把 staging 指向任意路径、symlink 父目录、允许 runner 写 release、仍用 PROJECT 默认当生产 state。

## 10. Receipt Reconciliation Contract

Step 2.2 SHALL 提供受保护内部查询 `lookup_receipt(job_id, expected_package_id, expected_package_digest)`；前两个类型 Identifier，digest 为 SHA，job 必须已有 pin。调用 peer 必须是 Controller/reconciler；用户侧需要新的有效身份且 owner 等于原 job.principal/task owner。MUST NOT 让用户按任意 digest 枚举。查询 SHALL 从 registry 推导全部 task/batch/source/revision/attempt 绑定，不能接受用户替换这些字段。

查询在 authorization.lock 下读取权威 acquisition DB（一致快照），检查 source_packages 的 `(task_id,batch_id,source_id,revision)`、package_id、package_digest 和 content_fingerprint，并与 outbox/job pin 核对。`package_digest = SHA256(现有 C(package))`，artifacts 的 size/hash 已在 package 中且 commit 时校验；不是 content_fingerprint，不是帧整体 hash。历史不存在 outbox 时遵守 §5.3 受限兼容对账。

| receipt status | 精确定义与后续行为 |
|---|---|
| committed | 权威 row 完整匹配；返回既有三字段 receipt，只确认历史，不读取正文/artifact |
| not_committed | 锁下权威 DB 可读且无匹配 source/package；若仍有有效 worker，查询须明确它未来仍可能提交，不得当永久失败 |
| pending | DB/锁不可用或一致性无法确认；保持 reconciling，不创建新 fetch |
| conflict | source/package 有 row 但绑定或 digest 不同；隔离 job，不能覆盖或返回他人 receipt |

准备将 not_committed 转 terminal 时 SHALL 在同一 guard 持有期于 registry 设置阻断状态/取消，确保查询后旧 worker 不再能 seal；已经 committed 则不回退。metadata-only receipt 查询 SHALL 不依赖当前 policy grant/generation、task cancellation、job deadline、sealed claim lease；它仍依赖 peer 与历史 owner 身份验证。MUST NOT 延长 sealed claim、重新 claim 已 sealed source 或仅为查询调用 acquisition._check(sealed)。

ACK 丢失：保持原 spool/pin，先 query；committed 结束，不必重传；not_committed 且仍授权/claim 有效可重发相同 spool；过期 sealed claim 通过 query 确认，不走旧 receiver replay。现有 wire replay 只对仍有效的 sealed claim 返回 ACK，receiver wire v1 不扩展 receipt query frame。cancel/revoke 在 package commit 后发生，查询仍 committed；禁止借 receipt 开新读取/refresh。用户不再有 root grant 不影响其确认自身历史提交，但内容访问须单独当前授权。

committed registry receipt SHALL 与 package 同期保留，V1 无自动清除。replay spool SHALL 在确认 committed 后可立即删除；未确认的 pinned spool 最多保留到 deadline+86400000，清理前必须先对账；数据库不可用时 SHALL 隔离且停止接受新 spool，不能删除唯一恢复依据。未 pin staging 按 job/attempt/fence 确认无活跃 owner 后清理；MUST NOT 仅按 mtime 删除可能活跃文件。

正常：ACK 丢失、sealed lease 到期、root 已撤销，原 owner 新身份查询得到 committed receipt。拒绝：凭该 receipt refresh；其他 owner 查 job；digest mismatch 返回 success；DB busy 当 not_committed 重抓。

## 11. Python Runtime Contract

Python 3.10 SHALL 可以作为受限 V1 生产候选基线；当前 fallback 每次 materialize 单个附件，单附件 <=10MiB、source 附件合计 <=100MiB、JSON <=16MiB、batch <=256MiB、单活动 writer 和既有所有结构预算 MUST 保持。Python 参数绑定、SQLite native allocation/cache、canonical JSON 多副本和 offline parser 不能被忽略；MUST NOT 用 tracemalloc 或单个 10MiB buffer 推导进程 RSS。

Step 2.5 SHALL 在受保护等效离线环境、真实 SQLite、真实 Unix socket、Python 3.10 下测试真实可被 schema 接受的最大 package/batch、近最大 JSON、最大附件组合、多 job 排队、取消竞争、磁盘满、commit 前后 kill、ACK 丢失。SHALL 记录整个服务进程峰值 RSS（包括 SQLite native）、SQLite BEGIN 到 COMMIT 时长、authorization.lock 与 Store lock 等待、cancel 从受理到阻止新请求及终止在途流的延迟、staging/spool/DB journal/WAL 峰值临时磁盘占用；不同合法最大组合不能被简化为单一小 fixture。

以下无充分当前证据，均明确为 **deployment acceptance parameter**，MUST 由 Step 2.5 实测报告与管理员部署预算填写，未填/未通过则生产 NO-GO：`max_rss_bytes`、`max_sqlite_transaction_ms`、`max_guard_wait_ms`、`max_store_lock_wait_ms`、`max_cancel_latency_ms`、`max_temporary_disk_bytes`（均 P63）。部署 `MemoryMax`、disk reserve、queue capacity 和 lease/heartbeat 配置 MUST 根据报告留足余量；MUST NOT 在本文虚构数值充当通过阈值。§5 的 60s/20s 只是默认协议时序，若验收不满足 SHALL 调整经验证的部署参数或继续 NO-GO，不降低取消/提交检查。

Python 3.11+ `sqlite3.Connection.blobopen` SHALL 仅作为后续可选优化；Step 2.5 要单独运行真实 BLOB 写入、回滚、崩溃、RSS 及跨版本兼容测试，模拟 blobopen 不算。真实集成通过前 MUST NOT 改变生产基线假设；即使通过也不自动提高 package 限制或宣称 JSON/SQLite 内存问题消失。

正常：3.10 最大合法输入测得 RSS/锁/取消/磁盘数据，全部低于已批准参数才生产验收 GO。拒绝：tracemalloc 小于 21MiB 就设置 MemoryMax；仅模拟 blobopen 便切 3.11 生产。

## 12. Step ownership matrix 与编码边界

| 契约/交付物 | Step 2.1 | Step 2.2 | Step 2.3 | Step 2.4 | Step 2.5 |
|---|---|---|---|---|---|
| 身份与两层幂等 | strict DTO、可信 peer 注入接口、replay ledger、Application bridge、owner/version 映射；合成 update 测试 | 真实受保护 Gateway peer transport 验证/接线；不实现真实 Telegram 网络接入 | 无身份来源扩展 | 无 | 身份隔离负向验收；实际 Gateway 接入仍需独立部署审查 |
| policy/generation | strict JSON、canonical digest、CAS epoch、allow resolver、generation history、verification 元数据 | seal 时读取权威 registry | 每 API 调用/恢复复核，typed adapter 接口不读真实 Token | 下载能力沿用 job 绑定 | 轮换/disable/revoke 竞态演练 |
| registry/claim | 新内部 DB、状态机、semantic key、claim port、guard/锁顺序、fake acquisition、recovery planner | claim 真接线、commit guard、acquisition outbox/migration、receipt query/reconciler | worker 执行及 heartbeat 调用 | pin/spool/framed sender | kill/restart/lease/cancel 完整恢复压测 |
| revoke vs seal | 本文模型落成接口并用 fake transaction 验证排序 | 修改最终 commit boundary；涵盖失败 package、replay、task cancel；不是 wrapper-only | 取消活动 API，旧 epoch/generation 禁止继续 | 停下载/发送，保留对账依据 | 真并发/崩溃证明本地线性化 |
| reference | 新严格 parser，bridge 传 ID32，旧 parser 不作为安全入口 | 验证 job/HELLO 根一致 | root type/parent/scope proof | 使用规范 root | 全链路边界测试 |
| property locator | 错误/DTO 和本文算法引用，无 Notion API | 无 | 算法、namespace、raw ID 处理、collision test | 消费安全 locator，package 全局唯一性检查 | 确定性 replay 验收 |
| API/scope | 仅 typed ports，不做网络 | 无 Notion 请求 | fixed API/version、分页、重试、scope walker，fake API；不给用户 raw fetch | 与 downloader 的有界能力接口 | 无真实 Token 离线联调；live canary 独立批准 |
| attachments/package | 保留现有 schema/gate，不实现下载 | 保留 wire v1/半关闭、deadline、peer、有限 writer | 提供已授权附件元数据 | 隔离 downloader/SSRF、builder、无凭据 spool、稳定 bytes | DNS/egress/内存/磁盘验收，图片 gate 回归 |
| paths | RuntimePaths/Owners 类型、验证接口、项目内 fake 测试 | Store/storage roots 解耦与 receiver staging 注入，保护无退化 | worker 配置注入 | downloader/spool 路径生命周期 | systemd/release/迁移模板及权限验收，不自动部署 |
| errors/audit | 封闭 enum、优先级、Application mapping、脱敏审计 DTO | 固定 seal_rejected、内部分类接线 | API/分页/proof 分类 | 下载/builder 分类，只用现有 gaps | 全链路脱敏与故障恢复验证 |
| runtime | 固定 3.10 兼容 API，不使用必须 3.11 的语法/库能力 | 单 writer 真 socket/SQLite 集成 | 有界 fake response | 有界 staging | 真实最大 RSS/transaction/lock/cancel/disk、3.11 可选分支验收 |

### Step 2.1 可修改文件建议

SHALL 优先新增以下模块（文件名是建议，接口/责任是强制）：`src/tg_testcase/telegram_identity.py`、`scope_policy.py`、`credential_registry.py`、`job_registry.py`、`authorization.py`、`notion_reference.py`、`internal_errors.py`、`runtime_paths.py`、`controller_bridge.py`，以及对应 `tests/test_*_contract.py`。新 registry migration SHALL 自包含在 job_registry 或新 `registry_migrations.py`，MUST NOT 复用/改写现有 acquisition migration version。测试数据 MUST 仅合成，测试临时文件 MUST 留在项目目录。SHOULD 新增 Step 2.1 delivery 文档记录实际完成和仍未接线项。

Step 2.1 明确禁止修改范围：现有 `protocol.py`、`application.py`、`acquisition.py`、`receiver.py`、`store.py`、`storage.py`、`migrations.py` 的执行语义；`schemas/`；现有 notion-source-package-v1 / sanitized-content-v1 和 receiver wire；`gates.py`、图片处理及 completeness/formal gate；生产系统文件、Agent 自身、真实 credentials。新 bridge SHALL 调用现有 Application，guard 与真实 acquisition 接线明确留 Step 2.2，不能为了让 Step 2.1 测试通过削弱旧校验。Step 2.1 无网络/Notion/socket 部署需求。

已确认代码差距 SHALL 依责任落实：当前宽松 validate_request/reference_id -> 2.1 新安全入口；当前 _commit_staged 无 policy/generation -> 2.2 最终 guard；当前 sealed lease 过期拒绝 replay -> 2.2 metadata receipt lookup；当前 receive 新建 importer 不注入 staging、Store/storage 耦合 PROJECT -> 2.2；raw property ID 非 UUID -> 2.3 locator、2.4 消费；真实最大 RSS/lock/cancel 和 blobopen 未验收 -> 2.5。本文不以修改源码“补齐”这些差距。

正常：Step 2.1 合成 revoke/claim 竞态测试通过但交付报告仍标最终 seal 未接线。拒绝：仅有 wrapper precheck 就宣称生产授权安全；在 Step 2.1 改 package schema 或顺便部署。

## 13. 静态完整性检查与 Freeze 判定

本次静态检查 SHALL 确认：12 项契约齐全；关键契约均有正常/拒绝例；规范用语、strict 字段、时间单位、双重幂等、generation、commit 线性化、状态机、outbox、grammar、错误映射、locator、路径分工、receipt 和 runtime acceptance parameter 完整；与现有 identifier/actor/claim/receipt/package digest 代码约束一致。SHALL 校验本文 JSON 示例、reference 表正常/拒绝向量、locator 长度以及错误 enum 与正文引用一致，并核对原有 src/schema/docs/tests 内容哈希未变。此为文档静态检查，不代替 Step 2.1 单元测试或 Step 2.2/2.5 集成验收。

SHA256 SHALL 对最终本文 UTF-8 文件 bytes 在外部交付报告给出；MUST NOT 将自引用 hash 写回本文造成循环。

**Contract Freeze：GO。Step 2.1：已达到“无需猜测安全边界即可离线编码”的标准。** 精确实现选择、原子边界和后续接线归属已固定，模块内部实现方式可自由但不可改变契约。部署实际 UID/目录、Gateway 真实认证来源、附件精确出口策略和资源验收参数仍属明确后续配置/验收阻断项，不阻止 Step 2.1 合成实现；MUST NOT 用测试默认值冒充生产审批。

**生产接入：NO-GO。** Step 2.2 最终 seal 接线及对账、Step 2.3/2.4 获取边界、Step 2.5 资源/权限/恢复验收完成前不能改变此结论。本次不修改源码，不运行网络或真实凭据路径，不部署。
