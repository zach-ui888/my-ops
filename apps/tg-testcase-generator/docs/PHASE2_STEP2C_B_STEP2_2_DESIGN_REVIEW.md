# Phase 2 Step 2C-B Step 2.2 Design Review

日期：2026-10-02。交付性质：仅设计与静态代码审查；没有实现、迁移或部署。

**结论：Step 2.2 离线实现设计 GO；现有代码直接生产使用 NO-GO。** 最终安全边界必须下沉到 acquisition SQLite COMMIT，不能仅包裹 receiver。两库使用同一 AuthorizationGuard 排序，以 acquisition 内同事务 outbox 记录提交事实，再由 Controller 幂等对账；不承诺两库原子提交。

唯一规范依据为完整阅读的 [DESIGN](PHASE2_STEP2C_B_STEP2_DESIGN.md)、[CONTRACT](PHASE2_STEP2C_B_STEP2_CONTRACT.md)、[Step 2.1 DELIVERY](PHASE2_STEP2C_B_STEP2_1_DELIVERY.md)。冲突以冻结 CONTRACT 为准。本文的模块名、event_id 算法和内部 transport framing 是实现细化建议，不改冻结契约。没有使用外部资料。

用户提供 Git 基线 `e57d226ed54fa4bfec7e9e0d6a7b952e7576d8f5`；本目录执行 Git 检查返回 `not a git repository`，因此不能独立证明文件与该 commit 一致。以下发现针对当前可见源码，不冒称完成基线验证。273 tests 是 Step 2.1 交付记录，本次没有重新运行。

## 1. 范围及当前实现证据

CONTRACT §12 明确把以下事项归 Step 2.2：真实 Unix peer transport（包括 Gateway 接线）、receiver 半关闭/deadline/有限 writer、真实 claim adapter、最终 commit guard、acquisition outbox/migration、receipt query/reconciler、Store/storage capability roots 和 staging 注入。**Unix socket / SO_PEERCRED 不是推迟到后续 Step 的项目。** 本 Task 仅设计这些事项。

Step 2.3 保留 Notion typed client、worker 执行/heartbeat 调用、scope walker、property locator；Step 2.4 保留 downloader、SSRF、builder、真实 spool/framed sender 生命周期；Step 2.5 保留完整恢复压力测试、最大 RSS/锁/磁盘测量和管理员部署模板。Step 2.2 使用合成 pin/spool 与测试 sender 验证接口，不提前实现上述生产组件。

| 当前代码位置 | 核对结果及 Step 2.2 必须补齐的边界 |
|---|---|
| `acquisition.py:OfflineAcquisition._commit_staged` | 事务内重查 claim、task cancel、source revision/root 与预算，原子写 package/artifacts/sealed/manifest；没有 registry 授权、owner 校验、最终 deadline/lease 复核或 outbox。`_check` 不是完整生产授权检查。 |
| `acquisition.py:claim/heartbeat/transition` | claim 返回值没有 lease/state，heartbeat 无返回值，时间用秒；registry port 需要毫秒完整 claim。不能直接把方法名相同视作已适配。claim UPSERT 不更新 revision，生产 adapter 必须发现不一致并拒绝，不能自行修正旧 revision。 |
| `receiver.py:ReceiverAcquisition._commit_staged` | replay 先查询 package，再 `_check(sealed)`，要求有效 lease；查询与首次 seal 分开锁区，无 guard。不能作为历史对账接口。 |
| `receiver.py:receive` | 全流校验、canonical package、COMMIT 后真实 EOF 才封存的 primitive 已有；但内部重新实例化 `ReceiverAcquisition`，会丢失外部保护 importer 和 staging 注入。无 socket peer/超时/并发管理。 |
| `streaming.py` | staging 先完成，BLOB 在事务内从本地文件写入；3.10 fallback 单附件 materialize。Staging 仅检查根目录，不能代替 RuntimePaths 父目录及 descriptor 安全。 |
| `authorization.py` | guard 使用稳定锁文件上的 flock，decision 仅当前持锁期有效；`authorize_commit` 检查 registry 与传入 claim，但不能证明传入 claim 是 acquisition 当前行，亦未绑定完整 staged 对象。 |
| `job_registry.py` / `job_contract.py` | intent、pin、状态机、attempt/fence、ACK/outbox consume、恢复 planner 已实现；`transaction()` 是 BEGIN IMMEDIATE，`get_job/authority` 也走它。Receiver 不能实例化该写入/迁移对象充当只读 reader。 |
| `scope_policy.py` / `credential_registry.py` | default deny、根唯一映射、policy digest、head/history、verification 校验与原子发布已有；必须每次最终提交取权威值，不用进程启动时缓存。 |
| `controller_bridge.py` | Application 操作外层已持 guard，task cancel 后传播 registry stop；两库间崩溃仍可能留下未传播的 task cancel。最终 seal 及 recovery 必须读取 acquisition task 状态。 |
| `controller_ports.py` | typed AcquisitionPort、SourceSnapshot、outbox DTO、lease floor、DeadlineBudget 已有，实际适配器未实现。 |
| `runtime_paths.py` / `telegram_identity.py` | 路径验证/Peer 注入契约已有；实际安全打开和 OS peer extraction 未实现。identity 的 clock high-water 不能自动代替 receiver deadline budget。 |
| `registry_migrations.py` | registry v1 已有 consumed_events，无须为了 outbox 重建 registry schema；不能与 acquisition version 混用。 |
| `migrations.py` / `store.py` / `storage.py` | acquisition v3 已有不可变 package/artifact/manifest 与三字段 receipt 元数据，但无 job/epoch/generation 关联事件；路径仍耦合 PROJECT。 |

相关测试已静态核对：`test_streaming.py` 的全流、尾随数据、重放、输入中取消/claim 改变、回滚、预算与模拟 blobopen；`test_notion_package.py` 的 acquisition 状态机、不可变数据、刷新与 gate；`test_step21_contract.py` 的 fake acquisition/receipt、guard 排序、outbox 幂等和恢复模型。`test_guard_is_held_through_fake_acquisition_sqlite_commit` 写的是单独 fake 表，不是实际 `_commit_staged`。这些现有证据不能替代 Step 2.2 接线验收。

## 2. 权威来源与不可变绑定

| 数据 | 唯一权威来源 | 不允许作为替代依据 |
|---|---|---|
| principal、job/source 绑定、pin、deadline、cancel、job state | Controller registry jobs，经严格 DTO/semantic_key/索引一致性校验 | HELLO、runner JSON、缓存副本 |
| epoch/digest、grant/root type/workspace、credential generation/enabled/deleted、verification | registry current_policy + head/history + verifications，重算 canonical digest | package.policy_version、旧 grant、仅 Token 可访问 |
| owner、task cancel、当前 source/revision/locator、batch 状态与 membership | acquisition tasks/source_inputs/collection_batches；tasks 列与 payload 不一致即拒绝 | job 创建时的 owner、JSON task 快照 |
| worker/attempt/fencing/status/lease | acquisition source_fetch_runs；registry 为受保护镜像，二者必须匹配 | HELLO attempt/fence、job 单边计数 |
| 已提交 package 与 receipt | acquisition source_packages + 同事务 outbox；artifacts/manifest 是该事务效果 | registry committed 字样、sender 自报 ACK、spool 存在 |
| root type | 受保护 policy/job；package 必须匹配 | source locator 本身不能证明 root type |

任务普通编辑导致 task.version 增加不让 acquisition 自动失效；task_expected_version 仅审计，不能作为长期 seal CAS。最终新提交检查 source_inputs 的固定 batch 绑定，并与 task 当前对应 source 的 revision/locator 比较；历史 receipt 查询只比对历史 pin/行，不因 task 已刷新而否认旧 commit。

所有 owner/root/revision/claim 变更的受保护入口也必须遵守 guard。现有业务没有 owner 转移 API，不新增此功能；若未来受保护维护路径改变 owner，旧 job 在最终检查必须失败。跨进程任意 SQL、runner 能修改 DB/锁文件的环境不满足此信任模型。

## 3. 提议接口与生产组合

| 模块/接口建议 | 职责与调用权限 |
|---|---|
| `controller_ports.py:RegistryReadPort` | 只读权威 job/policy/head/verification snapshot；需要已持 guard。保留 Step 2.1 的类型与校验语义。 |
| `registry_reader.py:RegistryReader` | 只读 SQLite 打开，query_only、短一致读事务，不迁移、不 BEGIN IMMEDIATE、不使用 immutable 缓存假设。共享纯校验逻辑，避免复制另一套宽松 policy parser。 |
| `acquisition_adapter.py:ProtectedAcquisitionAdapter` | Controller 内实现 AcquisitionPort，实际 inspect/claim/lookup/transition/heartbeat/receipt；无任意路径/SQL 参数。所有 mutation 要求外层 guard。 |
| `protected_acquisition.py:ProtectedReceiverAcquisition` | 受保护 importer，依赖 guard、RegistryReader、明确 staging capability、clock/budget；真正改造 acquisition 最终提交公共内核，而非仅子类 wrapper precheck。 |
| `receiver_service.py` | Unix socket peer、HELLO job lookup、bounded reader、writer admission、调用 receiver primitive、锁外发送固定 ACK/ERROR。 |
| `receipt_reconciler.py` | Controller 唯一 registry writer 路径：查 receipt、consume outbox、恢复状态。Receiver 不写 registry。 |
| `gateway_transport.py` | 同机 Gateway peer 到既有 IdentityLedger/ControllerBridge 的接线；不连接 Telegram 网络。 |

生产 Store/composition 必须标记“只允许 protected acquisition 写 package”。最终公共写入内核在生产配置下缺 guard、当前 decision、可信 job binding 或 outbox context 即拒绝。不能只靠不导出类名或调用者自觉；`seal`、`seal_stream`、`seal_failure`、Receiver replay 均纳入检查。离线 fixture Store 可以显式启用旧 primitive，生产配置不存在从缺省参数退回 fixture 模式的路径。Python 私有对象只是可信代码内部能力，不声称能隔离在同一服务中执行任意 Python 的攻击者；真正隔离来自 UID、不可写 release 与 DB 权限。

`receive()` 必须使用受保护组合层注入的 importer/staging，不得重新建立未保护 importer。HELLO 的 job_id 仅用于查找，所有 BINDING 字段逐项与权威 job/claim 比较；worker_id 从 registry/acquisition 得到，绝不补入 v1 wire。package_length/hash 必须等于 pin，并验证 canonical bytes/hash。服务在 HELLO 后的预检查只负责早拒绝，最终 `_commit_staged` 必须重新检查。

HELLO 只解析一次：将 primitive 的内部接线改为在校验 HELLO 后调用受保护 resolver，取得 job/claim/importer；resolver 的短暂 guard/DB 读取必须在继续读 PACKAGE 前结束。不能由 service 消费 HELLO 后再让现有 receive 从 PACKAGE 开始误读，也不能切换另一个 buffered reader。resolver 只能来自可信组合层，不是请求字段；离线测试保留显式 fixture 接口。

### 3.1 claim、heartbeat 与 retry 适配

持 guard：读取 source/授权 → registry 提交 claim_pending intent → acquisition.claim → 从 acquisition 重读完整行 → registry 记录权威 attempt/fence/lease → acquisition transition(fetching) → registry fetching。任一 acquisition 调用期间不持 registry 写事务；guard 跨这些本地步骤持有。claim 返回 None 不派发，转对账/恢复。

真实 port 必须把 `status` 映射为 DTO `state`、lease 秒安全 floor 为毫秒，补足旧 claim 返回值。不要计算 lease_seconds 后等待 Store 锁再把相同 duration 加到较晚 clock：会越过 job deadline。需要把既有 claim/heartbeat 的事务内核支持内部 absolute lease cap，在取得 Store/BEGIN 后以新鲜时间计算并写入 `lease_until <= min(requested_until, job.deadline)`；读回确认。不向用户或 wire 添加字段。retry_at 到 delay 的转换同样在锁内计算，禁止延长约定等待或租约。

heartbeat 始终复核授权、source/task 及真实 claim，然后更新 acquisition，再更新 registry 镜像；崩溃导致镜像较短时不能推定续约成功，可受保护恢复读取实际 lease，但只能同 worker/attempt/fence，且不超过 deadline。有效性取两者较早值，绝不因镜像恢复延长 acquisition 行。

claim_pending 崩溃按固定 source/revision/worker 查询补全，未知绑定不接管。旧 lease 失效后新 claim 必须由 acquisition 增加 attempt/fence，禁止 registry 自增。submitting/reconciling 必须先排除 commit 再重新 claim；sealed 永不重新 claim。registry-only revoke 不需要将 acquisition 置永久 abandoned：未 sealed 且 source 仍有效时，终结旧 job并等待/受保护释放旧 claim 后可创建新授权 job。task cancel 已使 acquisition abandoned，则不能重用该取消 source。

## 4. 最终 COMMIT 算法及 TOCTOU 证明

### 4.1 持锁区间

全部 PACKAGE/ARTIFACT 输入、hash/size/canonical 校验、COMMIT frame 和真实 EOF 已完成，staging 文件固定且只供本次 seal 读取后，**才获取 AuthorizationGuard**。在 acquisition SQLite COMMIT 成功返回或明确 rollback/连接关闭完成后才释放；ACK、通知、清理文件在释放后进行。

推荐将最终路径拆为以下明确阶段（描述流程，不是本次实现代码）：

1. 无锁准备 canonical bytes/digest/长度、已校验且封闭的 staging handles；检查 monotonic budget。此时的授权结论不可带入事务代替复查。
2. 获取共享 guard。开启 registry 只读 snapshot，重新加载 job、current_policy、credential head/history、verification，并校验全部绑定与当前 acquire 授权，得到仅本次 guard token 有效的不可变视图。结束 registry DB 读事务；持续持 guard，所以所有受保护 registry mutation 都无法改变视图。
3. 获取 acquisition Store flock，BEGIN IMMEDIATE。在同一事务读取完整 task/source/batch/claim，查 package_id 及 source 唯一键，确认是否是新 seal、相同历史 replay 或冲突。禁止拿着 acquisition 锁再打开 registry 事务。
4. 新 seal：要求 job submitting 或可提交的 reconciling，pin 完整，claim fetching，owner/cancel/source/root/batch/预算均满足。以本事务读取的真实 ClaimRecord 调用 `authorize_commit`，registry 参数是第 2 步受 guard 保护的只读视图。不得调用会隐式 BEGIN IMMEDIATE 的原 JobRegistry 实例，也不得以 wire claim 传入。
5. 写 source_packages、artifacts、sealed claim、按既有 seal_inputs 规则更新 manifest/batch、插入 outbox。不是最后一个 source 时本来就不生成完整 batch manifest；保持现有行为。所有这些写入同一 acquisition 事务。
6. **所有 BLOB/manifest/outbox 写入之后，紧邻 COMMIT 再检查**下表中的时间和 acquisition 不变量，验证 decision 仍属于同一次 guard 与该 staged/job/pin 绑定。之后不得再执行耗时解析、文件读取、网络调用或排队；直接 COMMIT。
7. COMMIT 成功才构造可信 receipt。释放 acquisition 连接/Store 锁，再释放 guard；锁外回 ACK。registry 状态最终通过受保护 ACK callback 或 outbox 独立事务更新。COMMIT 抛出异常或连接丢失时不推断未提交，关闭连接释放锁后进入对账。

现有 decision 是 opaque token，额外在内部提交上下文绑定 job_id、pin digest、canonical length、claim tuple 和 staging 身份，避免同一次 guard 内把 job A 的 decision 用于 B。不能序列化或通过请求构造此上下文。

### 4.2 最终重读清单

| 时点 | 必须重新读取/检查 |
|---|---|
| guard 内、取得 acquisition 锁之前最后一次 registry 读取 | job 完整绑定/状态/cancel_state/last_error/receipt conflict、不可变 pin 三字段/spool 状态、deadline、worker/attempt/fence/lease；current epoch/digest/canonical bytes；credential head/history 的 generation/enabled/deleted/workspace；verification 及 principal→operation→grant→root type/id→workspace→credential 唯一映射 |
| acquisition BEGIN 后 | task owner/cancel（列与 payload 一致）、task 当前 source 与 source_inputs 的 task/batch/source/revision/root/kind、batch finalized 与 source membership/未废弃、claim worker/revision/attempt/fence/status/lease；package_id/source 两种唯一键冲突；batch 已用字节与每未封存 source 4096 reserve |
| 插入后、COMMIT 前 | 新鲜 UTC 与保守 monotonic budget；now < job.deadline、now < min(registry lease, acquisition lease)；同一事务再读 task owner/cancel、source/root/revision、batch 及 claim tuple（本事务已转 sealed 是预期，不能再调用只允许 fetching 的旧 `_check`）；写入总量/manifest/outbox 与验证绑定一致；guard/decision 有效 |

registry 不在第 6 步重新开启事务：其“最后读取”位于第 2 步，之后 guard 一直阻止写入。这是受锁保护的权威视图，不是过期缓存。如果视图无法验证，必须退出 acquisition 事务后重新按顺序开始，不能反向获取 registry 锁。数据虽不变，时间会流逝，所以最后一次 deadline/lease 检查不可省略。

### 4.3 保证及精确限制

policy revoke、credential rotate/disable/delete、verification revoke、registry cancel、attempt 替换均以 guard 下的持久化更新 COMMIT 为生效点。更新先 commit，则最终 seal 看到新状态并拒绝；seal 持 guard 先 commit，则更新只能后生效，历史 package 保留。请求已经到达或排队等锁不等于撤销已生效，不能提前返回成功。

task cancel 由 Controller 先持 guard，再让 Application 在 acquisition 事务 commit，释放 acquisition 锁后传播 registry cancel；中间崩溃时 acquisition task.cancelled 仍阻止 seal。owner/root/revision 的受保护修改同理串行化；无 guard 的合法旧 acquisition writer也不能穿过 BEGIN IMMEDIATE，但生产不开放这类旁路。

**“任何 COMMIT 前发生都不能提交”必须按 CONTRACT §4.2 的可实现精度解释**：deadline/lease 在最后一次检查时已过期则 rollback；检查之后 SQLite COMMIT 执行中自然越过时间边界，无法承诺中断已启动 COMMIT。不得宣称具有精确到 COMMIT 返回瞬间的截止保证。若用户要求该更强保证，需要单独变更冻结 Contract，本设计不擅自增加该承诺。实际 COMMIT 窗口由 Step 2.5 测量与部署预算覆盖。

两库间不使用 ATTACH、嵌套 context manager 或先写 registry committed 来假装原子性。唯一 package 提交事实是 acquisition COMMIT；registry 可以暂时落后。

### 4.4 failed package 与 replay

failed package 也是新提交，必须正常授权、pin、guard、预算、outbox；授权失效、cancel、digest conflict 不得自动调用 seal_failure 绕过拒绝。现有 seal_failure 每次随机 package_id 的行为不可直接作为生产 retry：受保护 Controller 在仍授权且尚未 pin 时构造一次合法 failed package并 pin；已 pin 不能替换成另一份 failed 内容。

wire replay 仍先消费并验证全部流及 EOF；guard 内核实当前授权、同 pin、同有效 sealed claim，再返回原 receipt，不写第二份 package/outbox。已 revoked/cancelled/expired 或 sealed lease 过期，wire replay 拒绝，使用独立 metadata receipt query。历史 query 可确认 commit，不提供新提交权限。

## 5. Durable receipt / outbox 最小迁移

现有 source_packages 已保存 task/batch/source/revision、package_id、package_digest、content_fingerprint，足以在完整 pin 比对下确认历史；但缺 job_id、提交 attempt/fence、policy epoch/generation 及稳定可重放事件。因此足够做受限兼容 lookup，**不足以完成生产可信事件对账**。

仅建议 acquisition v3 → v4 additive migration：新增 `acquisition_commit_outbox`，按 CONTRACT §5.3 包含以下 16 个字段，不修改 Source Package v1、wire v1、source_packages 列或图片/gate。

- event_id（Identifier，PK）、job_id（Identifier，UNIQUE）。
- task_id、batch_id、source_id（Identifier）、revision（P63），UNIQUE(batch_id,source_id,revision)。
- attempt、fencing_token、policy_epoch、credential_generation（P63）。
- policy_digest、package_digest、content_fingerprint（SHA）。
- credential_ref（Name）、package_id（Identifier）、committed_at（UTCms）。

采用 NOT NULL、计数/类型约束，package_id 唯一并引用 source_packages(id)；若采用 FK，连接必须开启 foreign_keys。增加 outbox UPDATE/DELETE 拒绝触发器，保持与不可变 package 同期保存。迁移事务失败回滚 schema/version，不填充猜测出来的历史 job/epoch。registry v1 已有 consumed_events，本次无需新增 registry 列。

建议稳定 ID：

`event_id = 'acq1-' + SHA256(C(['acquisition-commit-v1', job_id, task_id, batch_id, source_id, revision, package_id, package_digest]))`

长度 69，满足 Identifier。相同 pin 的提交事件保持相同 ID；attempt/时间不参与 ID，实际成功 attempt/fence 与 committed_at 只记录在事件内容。event digest 使用既有 `SHA256(C(完整 event))`。重放读取原事件，不重新生成时间；同 ID 不同完整内容为 conflict，不覆盖。唯一 source/job 约束是第二道防线，不依赖 hash 数学无碰撞假设。

Receiver 从权威 job、事务内 claim、已验证 package 生成事件；runner/HELLO 不能自报任何事件或 policy/generation 字段。`package_digest = SHA256(现有 encode/C(package) UTF-8 bytes)`，artifacts size/hash 已被 package 绑定且实际 bytes 已验；`content_fingerprint` 是 load_package 重算验证的内容摘要，两者不能互换。package_id、source binding 与两个 digest 必须一起比对。package.policy_version 不是当前 policy_epoch 的权威证据。

`committed_at` 是最后检查附近在事务内写入的受保护时间，表示提交事务的时间标记，不声称准确测得 COMMIT 返回时刻；事件只有事务真正 commit 才可见。

Controller 在 guard 下读取 outbox + 对应 package 元数据并验证一致性，释放 acquisition 读锁后，在 registry 单独事务内调用 consume_outbox，同时写 receipt/committed、attempt sealed 与 consumed_events。事务失败两者都不落库，下次重读。ACK callback 只接受可信 Receiver 连接上的已验证 ACK；runner 不可直接调用 accept_ack。本阶段可优先用 acquisition 查询再消费，避免把任意三字段 dict 当提交证明。

不用 acquisition delivered 标志，不依赖易丢失内存通知。按稳定 key 分页重扫、与 consumed_events 比对；不得把非单调 hash event_id 的一次 high-water 当永久游标从而漏掉后来的小 key。可恢复全量有界分页轮询，重复消费幂等。V1 不自动删除 package/outbox/receipt。

## 6. 查询、恢复与逐场景决策

`lookup_receipt(job_id, expected_package_id, expected_package_digest)` 是受保护 Controller/reconciler 内部 port；wire v1 不增加查询帧。用户侧调用需要新鲜可信身份且等于原 job principal 与 task owner，不允许按任意 digest 枚举。owner 变化时不能将旧 receipt泄露给新 owner或不再匹配的调用人；可信内部 reconciler 仍可记录历史事实。

持 guard → registry 读取 pin及历史绑定（不要求当前 grant）→ acquisition 一致读：同时按完整 source tuple 和 package_id 查找，核对 outbox/job/pin。不能仅查“这个 digest 是否存在”。

- committed：完整匹配，只返回原三字段 receipt；不读取/返回正文、artifact，不要求当前 epoch/generation、task 未取消、deadline 或 sealed lease。
- not_committed：权威 DB 可读且两个查询均无匹配/冲突行；只是该锁内时刻的事实。有效旧 worker 未来仍可能提交，不能在释放 guard 后直接宣布永久失败。
- pending：锁/DB 不可用、恢复未完成或一致性不能确认；保持 reconciling，禁止新 fetch/claim/resend。
- conflict：source/package/outbox 与 pin不一致；隔离并阻断新提交，不覆盖他人数据。

缺 outbox 的历史 package 仅在完整已 pin 的 task/batch/source/revision/package_id/digest 全部匹配时确认 receipt，验证持久化 content_fingerprint 格式/一致性；记录 integrity audit，阻止新获取直至受保护修复。不能合成未知 job 授权，也不因缺事件将现存匹配 package 报为 not_committed。有 outbox 无 package 或 claim sealed 无 package 是损坏，隔离/pending，不能视作可重新 claim。

not_committed 后转 terminal 或 retry_wait 时：同一 guard 持有期内，释放 acquisition 锁后再开 registry 事务写阻断状态，必要时以已释放 registry 事务后的 acquisition 操作使旧 claim失效。这样旧 worker 在查询与状态落库间无法 seal。现有 recovery_plan 只凭 registry 判断原因，真实接线还须 inspect_source，识别尚未传播的 task cancel、owner/root/revision 改变，避免返回错误 resend 建议。

恢复须区分未 pin job：公开历史 receipt port 要求已有 pin，不能让当前 recovery_plan 对所有 fetching job 无条件调用该接口。未 pin 且状态证明尚不能进入 protected seal 时，使用内部 source/claim inspection 恢复或终止；若发现意外 package/sealed 行则隔离，不能猜测绑定或补造 pin。已有 pin 的不确定提交始终先查历史 receipt，再检查当前 source 是否仍允许新操作，避免当前 task 已取消/刷新导致历史成功被掩盖。

下表假设 job 已 pin 并开始提交；若明确尚未发送，可按冻结状态机从 prepared 等直接终结，无须虚构提交不确定性。表中“条件重发”均表示先得到权威 not_committed、仍授权、source 有效、pin/spool 完整，并经 begin_submit；不是自动重复 fetch。

| 场景 | Registry 状态/恢复结果 | Acquisition 事实 | resend / reconcile / claim | spool |
|---|---|---|---|---|
| A. acquisition COMMIT 成功、ACK 丢失 | submitting → reconciling → committed | package/artifacts/sealed/outbox 已原子持久化 | 先 lookup/consume；禁止新 claim，不必重发 | 确认前保留；确认后可删 |
| B. sender timeout，不知 COMMIT | reconciling；pending 时保持 | 可能未提交，也可能已提交，超时不能判定 | 先查；committed 结束；not_committed 才条件重发；旧 lease 失效后才考虑新 attempt | 保留同 pin |
| C. policy 在 COMMIT 后 revoke | 即使先记阻断，reconciling → committed；已 committed 不回退 | 历史 package/outbox 不变 | 只需历史确认；不以 receipt 重新 acquire/refresh | 确认后可删 |
| D. policy 在 COMMIT 前已生效 | submitting → reconciling → revoked（确证无提交） | 最终授权拒绝，无新 package/outbox | 禁止旧 job resend；新 epoch job 另授权、旧活动 job 终结且 source 未 sealed 才可 claim | 保留到完成对账，再按保留规则清理 |
| E1. job cancel 在 COMMIT 前已生效 | reconciling → cancelled（确证无提交） | registry requested 即阻止；task cancel 可使 claim abandoned | 禁止旧 job resend；取消 source 不重新 claim | 先对账后清理 |
| E2. cancel 在 COMMIT 后 | reconciling → committed 或保持 committed | sealed/package 保留，取消不撤销历史 | 仅确认，不重新 claim，不输出正文 | 确认后可删 |
| F1. receiver 收到 package 前 crash | 已发送则 reconciling；明确未发送则原 prepared | 无该次写入；仍查以排除更早提交 | not_committed 后条件重发；有效同 claim复用，过期则有条件新 attempt | 已 pin 保留 |
| F2. staging 中 crash | reconciling | 无该次 SQLite 写入，有孤立 staging | 同 F1；不能把 staging 当 receipt | pin保留；确认无活跃 owner 后清 staging |
| F3. SQLite transaction 前 crash | reconciling | staging 完成但无新 commit | 同 F1，须先查先前尝试 | pin保留；staging 可重新从同 spool接收 |
| F4. transaction 中 crash | reconciling，DB 恢复前 pending | 未 commit 部分应回滚；若 crash 恰跨 COMMIT 边界，实际结果须查 | 完整 receipt则 committed；确证无提交才条件重发/新 attempt | 对账前保留 |
| F5. COMMIT 后 ACK 前 crash | reconciling → committed | 同 A；OS 自动释放 guard | 消费 durable outbox，禁止新 claim | 确认后可删 |

deadline expiry、lease loss、旧 attempt/fence、owner/root/revision 改变导致发送结果不确定时也先 reconciling。确证无提交后的 terminal 原因优先 cancel → revoked → expired → failed；内部错误按 CONTRACT §7 单独排序。policy/credential 已失效时绝不通过新 lease 重启旧 job。

未确认 pinned spool 最多保留到 deadline+86400000；清理前必须对账。DB 不可用则隔离、停止接受新 spool，不能机械按 TTL 删除唯一恢复依据。confirmed committed 可立即删除 spool；pin/receipt 元数据继续保留。未 pin staging 根据 job/attempt/fence 和活动 owner 清理，不能只看 mtime。Step 2.2 输出清理决策及接口，真实 Producer spool 文件删除生命周期属于 Step 2.4。

## 7. 锁顺序与死锁边界

固定顺序为 **AuthorizationGuard → registry 短事务/读 snapshot → acquisition Store flock → acquisition SQLite BEGIN**。对两库的操作采用分阶段释放：不得把仍持有 acquisition 锁时打开 registry 事务误称“按顺序”；需要写 registry 时先释放 acquisition 事务和 Store 锁，guard 可以继续持有。

| 操作 | 锁与事务范围 |
|---|---|
| 新 seal/replay | staging 后 guard；registry snapshot 结束；Store/BEGIN；提交或回滚；释放 Store再释放 guard；随后 ACK |
| policy/credential/verification/cancel mutation | guard → registry 写事务 COMMIT → 解锁；不得先 registry transaction 再 guard |
| Application task cancel/refresh | guard；需要的 registry 读取结束；Application acquisition 事务结束/Store 释放；随后 registry 传播；不能在 Store 内 callback 到 registry |
| claim/heartbeat | guard；registry intent/读取完成；acquisition mutation完成；registry 镜像完成；无同时持有两库写事务 |
| receipt/recovery | guard；registry pin读取结束；acquisition 一致读结束；registry consume/terminal 写入；完全相同顺序 |

network/socket/file streaming、等待 EOF、ACK write、生产 spool 读取/写入、队列 admission 期间绝不持任一 DB transaction。staging 大文件输入也不持 guard；唯一必要例外是 SQLite BLOB 插入需要读取已经验证的 Receiver 私有本地 staging handle，此动作属于封存事务内的有界本地 I/O，不是外部输入流。这里不得打开 Producer 路径、等待其 flush 或锁。

filesystem staging/spool 的创建、打开、hash、flush 在数据库事务外；进入最终提交时不再申请 staging/spool 生命周期锁。通过已登记的活动 owner 与固定 handles 防止清理，清理器先在 guard 下判断并标记无 owner，然后锁外删除已隔离文件；不能持文件锁反向请求 guard。若使用 filesystem 操作互斥，必须在进入 guard 前完全释放，不能形成 file lock→guard 等待链。

同一进程组合层使用同一个 guard 实例支持现有可重入语义；同线程持一个实例再嵌套取得同 inode 的另一个实例可能自锁，禁止此用法。跨进程使用同一预建稳定 inode，禁止 rename/unlink/recreate。guard 等待可用非阻塞 flock + monotonic 有界重试，不用普通 lease 代替锁；超时按 pending/storage failure，不冒充未提交。

DB busy/磁盘故障有界退出并关闭连接；没有“等另一个服务持锁完成 RPC”的路径。receiver 不在 guard 内调用 Controller socket。crash recovery 和 migration 也在停止 admission 后遵守同一顺序；不能以恢复为理由倒置锁。

## 8. Protected Receiver / Gateway 最小 transport

Receiver：AF_UNIX/SOCK_STREAM；固定 receiver.sock；验证路径父目录/owner/group/mode 和 Linux SO_PEERCRED，peer UID 只能是配置中的 Producer UID。peer 从 accepted socket 提取，拒绝请求自报 UID；不把 PID 当持久身份。按权威 job 查 claim，peer 正确也不能凭空创建 job或 claim。

一连接一个 job/attempt，有限等待队列、一个活动接收/封存 writer；排队期间无 DB 锁，队列满有界拒绝。等待也计入 job budget。测试可使用配置中的测试 UID与本地 socket，不能把同 UID合成测试声称为真实部署隔离证明。

单一 reader 负责 frame header/payload/EOF，不混用 buffered reader 与 raw socket。每次 read 限制长度且重算剩余 monotonic budget；idle timeout 不能被每个小 chunk 无限重置总时限。采用 DESIGN 的初始默认：header/idle 10s、完整传输 180s、COMMIT 后 EOF 5s，并全部受剩余 job deadline 限制；这些不是性能通过阈值。发送 ACK/ERROR 也要有界（建议 5s），超时不改变提交事实。

客户端发送全部 frames后 flush（若有缓冲）并 shutdown(SHUT_WR)，保留读取方向。服务端 COMMIT 后 reader.read(1) **只有 b'' 才是 EOF**；timeout、EAGAIN、无数据、尾随字节均不允许提交。EOF 完整后进入第 4 节边界。ACK 仅在 commit/合法 replay 后发送；错误保持现有 `seal_rejected`，详细原因进入受保护 enum 审计，不回显输入或异常。

Gateway：固定 gateway.sock、SO_PEERCRED 建立 Peer，再按 issuer/bot_instance 注册映射和 audience 验证既有 envelope，调用 IdentityLedger/ControllerBridge。最小独立 framing 建议为 uint32 大端长度 + 严格 UTF-8 JSON，单连接一个 request/response，request 上限复用 MAX_ENVELOPE_BYTES=64MiB，响应设置显式有界配置；超限/截断在调用 bridge 前拒绝，不改 Application/receiver wire。完整读取后才进入 guard，回复在事务外发送。实际 Telegram 网络认证仍是后续独立部署审查事项。

receipt 是 Controller 内部 typed port；需要用户触发时通过已有可信 Gateway 身份控制面单独路由，不向 receiver v1 HELLO 增字段或接收任意 digest 查询。Step 2.2 不提供远程 HTTP/raw SQL/raw fetch 服务。

## 9. Runtime paths 与 Python 3.10

Store/storage 改为显式 capability root，不是删除 inside_project 安全检查。测试仍只用项目内目录；生产根只能由受保护组合层提供，缺 RuntimePaths/Owners 或保护验证失败即拒绝启动，不退回 checkout/store.root。

遍历采用 descriptor-relative、O_DIRECTORY/O_NOFOLLOW、安全 basename、fstat 类型/owner校验；覆盖 DB、Store锁、authorization.lock、SQLite journal/WAL/SHM、快照与 staging。Python sqlite3 接口不能直接以任意已有 FD代替 SQLite 路径及 sidecar 管理，不能假装一次 lstat 就消除所有路径竞态；数据库父目录必须不可被非可信主体替换，打开前后核对身份并依赖稳定受保护根。只读 registry 不使用 `immutable=1` 忽略更新。真实权限与 ACL 由部署验证器证明，不能由本次读取系统账户/凭据获取。

需要在后续部署审查明确一处冻结规范的实际配置张力：独立 Receiver UID 需要直接只读 registry，Controller 需要受保护 acquisition 操作，而 §9 的各 state root 固定 0700。仅创建独立 UID和这些 mode 不会自动得到跨 UID访问能力。Step 2.2 离线接口不能以 chmod world-readable、复制 registry 缓存、guard 内 RPC 或合并 UID绕过。管理员须提供经 Contract 审查认可的最小访问配置/能力交付方案；若需更改精确 mode/拓扑要求，先独立修订 Contract。该点阻断生产配置验收，不阻断项目内合成接线实现。

Python 3.10 是候选生产基线：仅用其支持的 sqlite3、fcntl、socket、selectors/线程及普通 context manager；不依赖 3.11 的 asyncio.timeout、TaskGroup 或 blobopen。保留 fallback 单附件 <=10MiB、总附件 <=100MiB、JSON <=16MiB、batch <=256MiB、单 writer 及全部现有结构预算。staging 完成不能保证 COMMIT 很短，guard期间 heartbeat/cancel 会排队，必须在 BLOB写完后再检查时间，不能在写大包之前验证一次就成功返回。

DeadlineBudget 从可信 deadline 建立并只缩短；同时检查 wall 与 monotonic。在运行中墙钟回退不得重建更长预算；重启丢失 monotonic 锚点时必须通过可信时钟恢复判定，无法证明时间可靠则停止新 commit/保持 reconciling，不能用较早 now 重新获得完整 15 分钟。identity clock_state 是辅助证据，不能仅因存在该表就宣称 receiver 已解决重启时钟问题。

Step 2.2 验证真实 Python 3.10 socket/SQLite 正确性及基本资源有界性，不用 tracemalloc 的旧结果填写生产参数。Step 2.5 必须实测并由管理员填写 max_rss_bytes、max_sqlite_transaction_ms、max_guard_wait_ms、max_store_lock_wait_ms、max_cancel_latency_ms、max_temporary_disk_bytes；未完成仍生产 NO-GO。3.11 真 blobopen 优化另验收，不提高预算。

## 10. Step 2.2 实现顺序与验收标准（待执行）

建议顺序：先只读 registry view/真实 acquisition port与路径能力；再 additive outbox migration和公共 commit内核；然后 protected receiver/socket与 Gateway接线；最后 receipt/reconciler、故障注入和既有回归。每步测试只用项目内合成文件/DB/socket，无 Notion、Token、部署或 Agent 修改。

| 必测组 | 通过标准 |
|---|---|
| 最终授权竞态 | 在 staging后、guard前、BEGIN前、BLOB后、COMMIT前设确定性 barrier；policy revoke、rotate/disable/delete、verification revoke、job/task cancel 两种排序均符合 §4；检查实际 package/artifact/manifest/outbox 行而非 fake 表 |
| source/claim变化 | owner/root/revision、worker/attempt/fence替换及 lease loss拒绝；task普通 version变化仍可提交；在大 BLOB写入期间耗尽 deadline/lease必须全事务回滚 |
| guard/锁顺序 | 两进程共享稳定 flock；新 seal、replay、failure、recovery、task cancel均覆盖；没有 acquisition→registry/guard反向等待，ACK及socket读取期间可取得 DB锁 |
| 旁路拒绝 | 生产 Store直接调用旧 seal/seal_stream/seal_failure、注入 fake trusted_job/decision、receive内部替换 importer均失败；fixture模式仍可运行旧测试 |
| 真实 port | 秒/毫秒边界、锁等待后绝对 deadline cap、heartbeat更新中断、claim intent中断、旧 UPSERT revision不一致、sealed不可重claim、同pin新attempt不重fetch |
| outbox原子性 | 每个插入阶段故障均回滚全套数据；commit后registry故障能恢复；稳定event_id、重复consume、同ID不同内容冲突、hash分页无漏事件 |
| 历史 receipt | ACK丢失 + sealed lease过期 + revoke/cancel/deadline过期仍确认；无正文返回；错owner/错pin/任意digest查找拒绝；缺outbox只按完整pin受限确认并隔离；DBbusy=pending |
| crash边界 | 本阶段基本进程kill覆盖矩阵F各点，重开真实SQLite后只有完整提交或完整回滚；不能靠捕获异常模拟所有kill。最大负载kill/restart演练仍归Step2.5 |
| Unix socket | 半关闭成功、无半关闭超时、尾随byte、截断/慢发、错peer、假UID字段、队列满、disconnect、ACK写失败；所有ERROR固定，真实peer extraction与注入单测区分 |
| 路径 | 未配staging拒绝、祖先symlink/可替换父目录/sidecar/锁替换拒绝、超长socket路径、只读registry不执行migration/DML、生产无PROJECT fallback |
| 不变规则 | Source Package/wire schema不改；PNG/JPEG原始bytes+descriptor、无OCR、visual_unresolved及 completeness/formal gate全部回归；failed outcome不能清除缺口 |
| 运行时 | Python3.10实际执行新增集成与既有全量测试，报告数量/失败/跳过；不得把历史273通过数当新实现验收 |

## 11. 本次交付记录

- 结果：完成 Step 2.2 设计审查，给出最终提交边界、真实 adapter/transport、最小 outbox migration、逐场景恢复与验收矩阵；未编写实现代码。
- 文件修改：本次开始时本文已存在；本次复核并补充 HELLO 单次解析接线与未 pin job 的恢复分支。未创建文件，未修改冻结规范、源码、schema 或测试。
- 检查执行：完整阅读三份指定规范，静态交叉审查指定实现及相关测试；未运行单元/集成/网络测试。273 tests 为 Step 2.1 历史交付记录，不是本次执行结果；不沿用原稿中无法由本次操作证明的文件前后哈希比较记录。
- 错误/剩余工作：当前目录不是可用 Git 仓库，基线未独立验证；本文接口/迁移/测试均待后续编码任务实施；跨 UID与0700根权限配置、实际部署资源参数仍须后续审查验收。生产仍 NO-GO。
