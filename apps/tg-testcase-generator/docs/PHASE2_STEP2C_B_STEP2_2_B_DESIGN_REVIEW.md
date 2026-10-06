# Phase 2 Step 2C-B Step 2.2-B Design Review

日期：2026-10-04。性质：实现前设计核对与实施计划；本轮只新增本文，不授权部署，不修改实现或冻结文档。

**结论：Step2.2-B 项目内合成、离线 transport 实现设计 GO；Step2.2-B 完成验收尚未执行；Production 继续 NO-GO。** 独立 Receiver UID 与 0700 registry state root 的访问冲突仍是 Production Deployment Blocker，本文不提供绕过方案。

## Current State

依据：[DESIGN](PHASE2_STEP2C_B_STEP2_DESIGN.md)、[CONTRACT](PHASE2_STEP2C_B_STEP2_CONTRACT.md)、[Step2.2 DESIGN REVIEW](PHASE2_STEP2C_B_STEP2_2_DESIGN_REVIEW.md)、[Step2.2-A DELIVERY](PHASE2_STEP2C_B_STEP2_2_A_DELIVERY.md)。冲突以冻结 CONTRACT 为准；本文只细化 transport，不取代上述文件。

用户提供 Step2.2-A 正式发布提交 `794d61ed3970324fefbc9ee4bac04cc5a6f202be`。当前项目 Git 命令返回 `not a git repository`，无法独立证明可见文件等于该提交。以下结论来自当前源码静态核对，不能当作 commit 校验。A 交付中的 322 tests 是历史结果，本轮没有执行这些测试。

| 可见代码 | 已有能力 / B 接线差距 |
|---|---|
| `receiver.py` | wire 为 1-byte type + uint32 大端长度；HELLO/PACKAGE/ARTIFACT/CHUNK/END/COMMIT/EOF；已有固定 ACK/ERROR 对象和 protected importer 保留。尚无 accept、内核 peer、socket deadline。 |
| `protected_acquisition.py` | `ProtectedReceiverAcquisition` 显式 staging、真实 job/claim 查询、最终 `_CommitBoundary`，复核 pin/授权/不变量/时间。B 必须调用它。 |
| `acquisition.py` | 输入在事务外；最终公共内核验证真实 boundary，在同一事务写 package/artifact/sealed/manifest/outbox。B 不另写 seal 内核。 |
| `registry_reader.py` | `mode=ro`、`query_only`、短一致读快照；没有 migration/DML；仅当前 guard 持有期内的 snapshot 有效。 |
| `store.py` / `storage.py` / `streaming.py` | protected marker 防止旧 seal 降级；仍有 PROJECT 耦合、路径检查与打开分离、staging 仅根检查等差距。不能称为生产 capability-root 接线已完成。 |
| `runtime_paths.py` | 冻结字段、服务独立 UID、0700 state、0750 socket root、0660 sockets 校验接口已存在；当前 verifier 为注入接口，不是实际部署证明。 |
| `telegram_identity.py` / `controller_bridge.py` | `Peer(uid)`、issuer/bot/audience、持久 replay、owner/version/授权桥已有；peer 必须由 transport 构造。 |
| `authorization.py` / `store.py` | 当前 flock 阻塞等待，不能因外层 socket timeout 就声称本地锁等待有界；B 需补齐有界锁获取接线且保留锁语义。 |
| `acquisition_adapter.py` | receipt 尚为 pending 占位；完整历史 receipt/reconciler 属 C。B 不把 wire replay 当历史查询。 |

## Trust Boundary

- codex-runner 是开发/非可信调用主体。可写 checkout、能构造任意 JSON 或同 UID 测试对象，都不产生生产授权。不能将 runner 配成 producer/gateway/controller/receiver，不能加入 guard/gateway/receiver 组，也不能把其可写代码加载进受保护服务。
- TG Controller 的 registry writer 与受保护 Application 组合层可信；Receiver 只读 registry，写 acquisition；Producer 仅获准提交无凭据 package。Receiver 不获得 Notion Token，不查询 Controller 授权 RPC。
- root/管理员是安装与权限配置的信任根，不是默认准入的 socket 客户端。生产服务 UID 必须非 0 且互不相同。现有 TG Controller 即使由 root 管理，也不能把 root 连接自动映射为 gateway/producer。
- Gateway → Controller 的 `gateway.sock` 只承载冻结身份 envelope；Producer → Receiver 的 `receiver.sock` 只承载 package wire。两者不是同一个协议，Gateway 不直接写 acquisition，也不接受通用 fetch/SQL/方法调用。
- 独立 Receiver UID 是未来部署前提，离线同 UID fixture 仅验证机制；测试注入 peer/verifier 入口不得成为生产参数、请求字段或失败 fallback。

## Socket Ownership / Permission Model

固定使用配置能力 `RuntimePaths.socket_root`，派生 basename 仅 `gateway.sock` / `receiver.sock`；不得使用请求/job 拼路径，不用 abstract socket、TCP 或自动发现。完整路径 encoded bytes <=107；配置路径遵守当前严格 ASCII、绝对路径、无空段/点段规则。

| 对象 | owner / group / mode | 使用边界 |
|---|---|---|
| socket_root | 冻结允许的 gateway/controller/receiver 服务 owner；0750；部署显式登记专用目录 group | 所有祖先不可由 runner/Producer 替换；获准客户端只有穿越权，无目录写权；group 与 ACL 必须验证，不能仅查末端 mode |
| gateway.sock | controller_uid / gateway_gid / 0660 | gateway_uid 为客户端，Controller 为服务端 |
| receiver.sock | receiver_uid / receiver_gid / 0660 | producer_uid 为客户端，Receiver 为服务端 |
| registry_state_root | controller_uid / 0700 | Receiver 跨 UID 直接读取目前未解决 |
| acquisition_state_root、receiver_staging_root | receiver_uid / 0700 | Controller 受保护访问 acquisition 的跨 UID 配置亦待解决 |
| authorization.lock | controller_uid / guard_gid / 0660 | Controller/Receiver 使用，Producer/runner 禁止；稳定 inode，永不 unlink/rename/recreate |

**共享目录张力：** 当前 RuntimePaths 只有一个 socket_root；0750 无 group 写权，独立 Controller/Receiver 不可能仅靠加入组都在其中 bind/unlink。socket_root 的 group 在当前 validator 中亦未固定。B 不新增 RuntimeOwners 字段、不改成 0770、不合并 UID。离线分别验证两端生命周期；生产同时启用两端前，须单独审查管理员预配置/监听 FD 交付或目录拓扑，明确谁创建、验证、删除两个节点。该候选方案尚未批准/实现；若改变冻结路径或 owner/mode，先 Contract Addendum。不能以两端各自测试通过宣称共享部署成立。

B 的生命周期接口须区分“有目录管理能力的本地测试实例”与“经审查的受保护 provisioner 提供监听能力”；生产缺能力时拒绝启动，不临时提权。启动流程要求：

1. 在 admission 前验证配置、祖先、目录 descriptor（O_DIRECTORY/O_NOFOLLOW）、owner/group/mode、ACL 和不可替换性；路径、权限不符则失败，不自动 chmod/chown 修复未知节点。
2. 用单实例生命周期互斥串行化检查、bind 和清理。互斥能力来自受保护组合层，不能复用 AuthorizationGuard 包住网络探测，不能依赖 PID 文件或 PID 存活独自证明归属。
3. 节点不存在才 bind。创建时 umask 0077，初始节点不向客户端开放；可信目录内确认刚创建 inode 后设置预定 owner/group/0660，再次验证并 listen。无权限设置 owner/group 则关闭本次资源并失败；不能继续使用宽松节点。
4. symlink（含 dangling）、普通文件、目录、FIFO、设备或 owner/group/mode 错误：拒绝启动，绝不覆盖、跟随、自动删除。任何祖先被替换或无法证明稳定也拒绝。
5. 旧 socket：只在生命周期互斥下、确认属于该服务的固定节点、无旧活动实例且有受保护停机/恢复证据时，核对 device/inode 后 descriptor-relative unlink，再 bind。连接成功意味着已有 listener；ECONNREFUSED 单独不足以证明 stale；permission error/timeout/身份不明都保留并拒绝启动。
6. 正常停机先停止 admission，关闭/排空有界连接，释放活动 owner；仅删除本实例记录且仍匹配 inode 的 socket。crash 留下节点按上一步恢复；不删除他人新建节点。authorization.lock 与 DB/sidecars 不作为 socket 残留清理。

Unix bind 没有通用 dir_fd 接口，lstat 后 bind 不能独自消除竞态。稳定、不可被非可信方改写的父目录是必要前提；B 不通过改变全进程 cwd 或用户路径技巧声称已修复所有竞态。

## SO_PEERCRED Authorization

accepted AF_UNIX/SOCK_STREAM socket 在读取应用字节、入应用等待队列、查询 registry 之前，调用 `getsockopt(SOL_SOCKET, SO_PEERCRED, sizeof(struct ucred))`。Linux ucred 提供 pid/uid/gid；按运行平台原生布局解包并验证返回长度/数值，不能使用 wire 大端帧布局解包。缺常量、失败、短返回或不支持的平台一律拒绝，不退回请求字段。

| 端点 | 允许 UID | GID 判定 |
|---|---|---|
| receiver.sock | 仅配置的 producer_uid | 内核 primary gid 必须等于 receiver_gid |
| gateway.sock | 仅配置的 gateway_uid，且 `(issuer,bot_instance)` 注册映射也精确匹配该 UID/audience | 内核 primary gid 必须等于 gateway_gid |
| 其他（含 UID 0、runner、downloader、Receiver 自身作为 Producer） | 拒绝 | 单有允许 group 不能授予身份 |

这是 B 的保守 transport 准入细化，使用现有 RuntimeOwners 字段，不扩大冻结 UID 授权。管理员须将连接进程有效组配置为对应专用组；仅 supplementary group 有 socket DAC 权限仍不足。SO_PEERCRED 不给出 supplementary groups，不能从请求自报组或 PID 路径补查猜测。空/缺失 allowlist 默认拒绝，不能默认当前 UID/GID。新增其他合法 primary gid 需要另行明确配置审查，不静默放宽。

pid 仅是这条连接的内核观测值，不作为持久授权、owner、worker_id 或反复查进程身份的依据。信任获准进程不把已连接 FD 交给 runner；不接受 SCM_RIGHTS/SCM_CREDENTIALS 作为替代授权接口。生产客户端同样验证固定 socket 节点与 server peer UID（分别 controller_uid/receiver_uid），防止把伪 ACK 当提交证明。server gid 按受保护部署登记核对，不从 socket 文件 group 推断 server primary gid。

HELLO/envelope 中自报 uid/gid/pid/peer 为未知字段，拒绝；认证后才构造既有 `Peer(uid)`，envelope 不能创建 Peer。

## Protocol / State Machine

用户目标中的 BEGIN 是概念起点，**冻结 wire 的实际起始帧是 HELLO(type=1)，不新增 BEGIN 帧或 protocol version**：

```text
accept → OS peer check → bounded admission
→ HELLO → authoritative resolver（短锁区结束）
→ PACKAGE JSON → (ARTIFACT metadata → CHUNK* → END)*
→ COMMIT → real read EOF → final Commit Boundary → fixed ACK → close
```

一连接一个 job/attempt 的一次提交，不复用、不 pipelining、不任意 method dispatch。帧号继续 HELLO=1、PACKAGE=2、ARTIFACT=3、CHUNK=4、END=5、COMMIT=6、ACK=7、ERROR=8。只允许预期状态的类型；客户端不能发送 ACK/ERROR。package 中现有 artifact path 是逻辑标签，不用于打开客户端路径。不得新增 URL/header、SQL、pickle、反序列化代码或文件能力。

HELLO 单次解析：在 `receive` 内已有严格 HELLO 验证后，调用可信组合层 resolver(job_id)，返回 `ProtectedReceiverAcquisition` 及权威 job/claim。resolver 不由请求指定；不得先消费 HELLO 再让 receive 误把 PACKAGE 当 HELLO，也不得复制另一套宽松解析器。既有 fixture 调用可保留；生产 service 必须强制 protected Store/reader/importer。early lookup 的所有锁在下一次 socket read 前释放。

客户端发送全部帧，若有缓冲先 flush，再 `shutdown(SHUT_WR)`，保留读向读取唯一响应。服务端所有 header/payload/EOF 经同一个 bounded reader；不混读 makefile 与 raw socket。COMMIT 必须空 payload，随后 `read(1)==b''` 才能提交。无半关闭等待至 EOF deadline 后拒绝；timeout/EAGAIN/暂时无数据不是 EOF。COMMIT 前 EOF、部分 header/payload、缺 END、缺 artifact、hash/长度不符全部拒绝。COMMIT 后任意字节（含第二个 COMMIT、第二个 package）均拒绝，不能先提交再查尾随数据。

同连接 duplicate COMMIT 是 malformed；新连接同 pin 全流重放仍须当前授权、有效 sealed claim、原 outbox 匹配，才返回同 receipt，无重复 package/outbox。不同 digest/绑定拒绝。发送完后直接 close 导致无法接收 ACK；如果服务端已经收到完整合法 EOF，仍可能提交，客户端不能把断连当作回滚证明。

Receiver 回包沿用现有对象：ACK `{protocol_version:1,type:"ACK",receipt:{package_id,package_digest,content_fingerprint}}`；ERROR `{protocol_version:1,type:"ERROR",code:"seal_rejected"}`，由既有 ACK/ERROR 帧包装，限制见下。固定指 schema/错误码，不是固定 receipt 值。不能增加内部原因、reconciliation 状态或认证详情；未经认证、队列满可直接 close，其他错误尽力有界发送固定 ERROR 后 close。

Gateway 独立 transport：uint32 大端长度 + 严格 UTF-8 envelope，单 request/response；请求完整读完并确认客户端 SHUT_WR/真实 EOF 后才调用 `ControllerBridge.handle`（其使用 IdentityLedger），避免尾随第二请求产生执行。响应同长度前缀包装既有 Application response，不新增 Application operation；保持 audience、issuer/bot、摘要、nonce/update 双幂等、owner/CAS 和当前内容读取授权。边界 framing 错误仅给既有固定通用 Application error 或关闭，不回显 JSON。真实 Telegram 接入与完整 receipt 控制面路由不在 B。

## Resource Limits / Timeouts

所有上限在分配/读取对应 payload 前检查；实际读取累计量仍须校验，不能信任声明长度。以下 transport 默认是离线实现参数，不是生产性能验收值。

| 项目 | 限制 |
|---|---|
| Receiver frame header | 固定 5 bytes；未知 type/超长声明先拒绝，不分配声明尺寸 |
| HELLO / ARTIFACT / CHUNK | 4096 / 512 / 65536 bytes；CHUNK 非空；END/COMMIT 长度为 0 |
| PACKAGE / ACK / ERROR | 16 MiB / 1024 / 128 bytes |
| artifact | <=50 个；单个 <=10 MiB；累计 <=100 MiB；metadata 与 package 声明精确匹配、无重复遗漏 |
| package JSON + artifact bytes | <=116 MiB；batch <=256 MiB，并保留每未封存 source 4096 bytes；batch 由最终事务权威核算 |
| 结构 | pages 100、depth 16、blocks 20000、单块文本 64 KiB、文本 8 MiB；保留现有 JSON depth 24 / objects 200000 等 parser 预算 |
| Gateway request / response | request 复用 MAX_ENVELOPE_BYTES=64 MiB；B 默认 response ceiling 64 MiB，超限不截断成成功，返回固定 storage_error/关闭；若动作已完成仍保留原 durable 幂等结果，不重新执行 |
| socket header / idle | 各阶段 header 总读取 10s；无进展 idle 10s；不能逐字节无限延长 header deadline |
| 单连接输入总 deadline | accept 时 monotonic +180s，含排队；HELLO 后再取可信 job 剩余 deadline 的较小值，只缩短 |
| COMMIT 后 EOF / Gateway 尾 EOF | 最多 5s，同时受总 deadline 限制 |
| ACK/ERROR/response write | 独立最多 5s，锁外；失败不改变已提交事实 |

小 CHUNK 不能通过巨量 5-byte headers 绕过累计限制：合法非空 CHUNK 数最多等于 artifact 总字节数 A；总 frame 数 <=3+2N+A，其中 N<=50、A<=100 MiB。入站总字节 cap 可取 `16MiB+4096+512*50+100MiB+5*(3+2*50+100MiB)`（另允许一次尾随检测 byte，只用于拒绝），并随已校验实际 package 声明收紧。这个保守推导保留合法 1-byte CHUNK，不擅自提高最小 chunk 或降低 schema 预算。header、payload、frame count、时间、累计 staging 均边读边计数；CPU 慢发同时受 180s 约束。

默认每端点 1 个活动处理槽 + 4 个已认证等待连接，listen backlog 建议 4；应用同时接受最多 5 条/端点，两端总计最多 10 条（不把内核 backlog 声称为精确资源配额）。等待连接不预读 payload、不申请 staging；等待最多 10s且计入180s，满即关闭，新连接不为旧连接延长预算。单 active Receiver staging 最多100 MiB、最多50个artifact handles；Gateway 同时最多一份64MiB输入/一份有界响应，JSON对象和副本额外计入 RSS。socket收发缓冲使用显式有界配置（离线建议每方向64KiB，核对内核实际值），拒绝按连接无限建线程或无界任务队列。

临时磁盘全局 admission 必须计入孤立 staging；一个活动 staging 的100MiB不是 SQLite/journal/spool 的总磁盘限额。无法确认 residue 所有权/空间预算则停止新 admission。生产 max_rss_bytes/max_temporary_disk_bytes 等仍由 Step2.5 实测确定，不用上述payload算式推算 MemoryMax。

每次 read/write 以 monotonic 剩余预算重算 timeout；慢客户端持续发一个字节不能续期。job DeadlineBudget 锚定不得晚于首次可信 job 查询；还须继承 accept/排队耗时，不能在 resolver/commit 重建180s。墙钟回退不延长预算；重启可信时钟未恢复则拒绝新 commit。

本地 guard/Store flock 获取须使用非阻塞尝试 + monotonic 有界等待，锁前结束或超时后释放；B 离线默认单次等待最多5s，且受剩余job budget限制，SQLite busy timeout不超过同一剩余预算。不得从另一线程“超时返回”却让后台继续不受控提交。此处不承诺中断已经开始的 SQLite COMMIT；最终时间精度仍遵守冻结 CONTRACT §4.2。

## Commit Boundary Integration

| 阶段 | 必须验证 | 不能承担的权威结论 |
|---|---|---|
| accept/read stream | OS peer、admission、frame顺序/长度、strict字段/类型、时限、HELLO绑定、canonical JSON/hash、结构预算、artifact实际字节/hash/去重、累计计数 | peer正确不等于job获准；HELLO不能创建job/claim |
| COMMIT 前及 EOF 后、最终锁前 | artifact集合完整、END/COMMIT/真实EOF、私有staging固定handles、复验hash/size、pin长度/digest早期一致性、剩余预算 | 不能用早期授权缓存替代最终授权；不打开Producer文件 |
| Step2.2-A final boundary | guard下RegistryReader最新snapshot；关闭registry事务后Store/BEGIN；owner/cancel/root/revision/batch/claim/attempt/fence、policy epoch/digest、credential enabled/deleted/generation、verification、pin/state/deadline/lease、唯一性/预算/failed/replay规则 | transport不得替代authorize_commit或自己写package/outbox |
| BLOB/manifest/outbox写完、SQLite COMMIT之前 | 再校验A已有acquisition不变量、guard decision、wall/monotonic deadline及两边lease较早值 | 不承诺打断已开始COMMIT；不再次反向打开registry事务 |

唯一持久提交事实是 acquisition SQLite COMMIT。ACK仅在成功新提交或合法完整 replay 后返回；registry committed/内存成功标记/连接关闭都不是 package truth。ACK丢失不删除package、不重新claim sealed source、不重新fetch生成不同bytes、不重复写outbox。

B负责证明ACK写失败后原package/outbox仍存在和当前合法replay幂等。客户端在发送结果不确定后保持原pin/spool并进入/保持reconciling；完整lookup_receipt、撤销/lease过期后的历史确认、outbox后台消费与terminal判定由C实现。B不能发送假not_committed，也不能把固定ERROR解释为历史未提交。测试可直接读取合成SQLite证明事实，这不是新增公开receipt接口。

## Crash / Restart Semantics

| 崩溃/断连窗口 | 要求 |
|---|---|
| accept前或认证/排队时 | 无该次acquisition写入；关闭FD，重启重新认证，不继承内存Peer/队列 |
| HELLO/PACKAGE/artifact读取或截断 | 无本次封存事务；正常异常清理本连接staging；crash residue不能当作package |
| COMMIT前、COMMIT后等待EOF | 不提交；完整EOF之前超时/尾随字节均拒绝；此前其他尝试是否提交仍需对账 |
| 完整EOF后、SQLite事务前 | 可有完整staging，仍不代表提交；重启不直接把残留seal |
| SQLite事务中/跨COMMIT窗口 | 重开真实SQLite仅允许完整原子提交或回滚；结果不确定保持reconciling，不能仅凭kill时间推断 |
| SQLite COMMIT后、ACK前/ACK部分写出 | package/artifact/sealed/适用manifest/outbox完整保留；ACK发送失败不能撤销；C对账确认 |
| ACK后、registry未同步 | acquisition仍是truth；C幂等consume，不重新fetch |
| listener crash/restart | OS释放FD/flock；socket节点可能留下；按受保护生命周期证明后清stale，不能先unlink再探测 |

正常失败的staging由当前owner锁外清理。crash residue须关联本地活动owner及job/attempt/fence；确认无活动owner后隔离再锁外删除，不按mtime单独清理，不追随symlink，不递归删除未知内容。B可实现保守“无法证明则保留并拒绝admission”；跨重启完整恢复决策由C补齐。Producer spool生命周期归Step2.4，B不删除它；package/outbox/receipt禁止自动GC。

## Concurrency / Lock Ordering

Receiver首版只有一个活动接收/封存writer；Gateway独立单活动处理槽，故慢Producer不占Gateway网络读取线程。两端共享同一授权锁inode，服务内嵌套调用共享同一guard实例；不同请求不得并发复用带`_connection`状态的RegistryReader对象，可按活动请求创建reader。

固定顺序：**AuthorizationGuard → registry短事务/snapshot（结束）→ Store flock → acquisition SQLite BEGIN/COMMIT → 释放Store → 释放Guard → ACK**。Gateway需要写registry时，先结束Application acquisition事务并释放Store，再打开registry事务；不能同时持两库写事务或Store→registry/guard倒序。Controller仍是唯一registry writer。

accept、排队、read、等待EOF、write ACK、Producer spool输入、staging创建/hash/flush/清理均不得持guard或任一DB事务。最终事务只允许从已固定、已验证的Receiver私有staging handles做有界本地BLOB I/O。lifecycle锁不与AuthorizationGuard嵌套；不在guard内connect/probe旧socket，不在持文件生命周期锁时等待guard。异常/超时/取消须释放槽位、FD、reader事务和锁。

不能仅通过mock的“锁已释放”断言证明网络边界：测试用第二进程在第一进程暂停PACKAGE、EOF、ACK期间成功取得guard/registry写事务/Store锁，并用barrier证明最终COMMIT期间撤销按同一guard排序。

## Logging / Audit Rules

只允许冻结审计白名单：封闭错误enum、受保护匿名principal别名、已验证job/request ID、policy epoch/digest、时间/耗时、计数/预算结果、固定endpoint模板。未认证连接只计聚合拒绝数，不记录其自报job/身份；限速聚合日志，不能让拒绝洪泛填满磁盘。

禁止credential/token/secret_handle、signed URL/query、raw sensitive headers（含Authorization/Cookie）、正文/标题、raw property/page ID、原始envelope/package/artifact、原始异常/traceback中的输入。peer pid/uid/gid保留内存用于决策/测试，不默认扩大冻结持久审计字段。日志/指标标签不得承载攻击者字符串；内部原因不能写receiver ERROR或package.gaps。用合成敏感哨兵覆盖成功/异常/timeout/拒绝日志检查。

## File Change Boundary

**本Task仅新增本文。** 下列是未来B编码任务的允许范围建议，不能解读为本轮修改授权：

| 范围 | 未来B允许的最小变化 |
|---|---|
| 新增 `src/tg_testcase/receiver_service.py`、`gateway_transport.py`、`unix_transport.py` | listener/peer、bounded reader/writer、admission、生命周期、固定响应接线；文件名可按责任合并 |
| `receiver.py` | HELLO单次解析的可信resolver接线；保留原wire及protected importer |
| `runtime_paths.py`，必要时新增内部path capability模块 | 实际no-follow/owner/group验证及明确能力注入，不改变冻结RuntimePaths/Owners字段或mode |
| `store.py` / `storage.py` / `streaming.py` | scoped capability root、安全打开/sidecar/staging、明确fixture路径兼容；不得删除保护检查或生产fallback |
| `authorization.py` / `store.py` / `registry_reader.py`，必要时 `protected_acquisition.py` | 有界锁等待/剩余预算传递/组合层接线，保持A最终校验和只读语义；不重写_commit_staged/授权策略 |
| 新增 `tests/test_step22b_transport.py`、`tests/test_step22b_paths.py`；必要时扩展A/streaming契约测试 | 项目内合成数据、真实AF_UNIX/SQLite、注入式拒绝测试；不改旧断言以掩盖退化 |
| 新增B交付文档 | 实测结果/跳过/生产blocker，不能沿用历史通过数 |

禁止修改冻结DESIGN/CONTRACT/Review、schema、migrations/registry_migrations、package/gap enum、formal completeness/gates、图片PNG/JPEG raw bytes+descriptor/无OCR/visual_unresolved语义、Application wire、receiver wire与receipt字段、A原子outbox与commit授权语义。requirements、deploy、systemd、Agent、凭据、生产目录不在B范围。若确需超范围修改，先提出具体设计差异；触及冻结要求则停止相关实现并提交Contract Addendum，不静默改规范。

实施顺序：①固定peer/limits/admission配置及默认拒绝；②项目内listener/path生命周期与bounded reader；③HELLO resolver接入A并保持单一reader；④Gateway接现有ledger/bridge；⑤有界锁和path capability接线、crash/日志清理；⑥运行下表与既有回归，产出离线交付报告。各步不依赖真实Token或解决生产跨UID权限。

## Test Matrix

所有fixture/DB/socket/log均在项目内；Python3.10真实执行，禁用字节码写出到项目外。真实peer读取测试与注入UID/GID单测分开报告，不使用sudo/setuid/其他用户账户；跨真实服务UID权限证明留受保护部署验收，缺少它仍生产NO-GO。

| 测试组 | 必须覆盖 / 可观察通过条件 |
|---|---|
| kernel peer / spoof | 真实AF_UNIX提取本进程pid/uid/gid；请求伪造peer/uid字段拒绝；getsockopt失败/短返回拒绝，bridge/seal未被调用 |
| UID/GID allowlist | 正确pair通过；UID错/GID对、UID对/GID错、只有supplementary组、空配置、UID0、runner、downloader均拒绝；两端角色互换拒绝 |
| socket permissions | root/socket/所有祖先owner/group/mode/ACL错误拒绝；0660前不admit；超107字节拒绝；不能自动修复未知节点 |
| framing | 各header/payload切点截断、未知/乱序type、重复HELLO/PACKAGE/ARTIFACT/COMMIT、非零END/COMMIT、空CHUNK拒绝；单字节分片合法流成功 |
| JSON / pin / artifacts | 重复键、未知字段、错误类型/编码/深度、noncanonical、hash/pin/metadata不符、缺artifact/END、多artifact拒绝；合法零长度artifact按原schema处理 |
| bounds | 每帧limit/limit+1、0xffffffff声明在分配前拒绝；附件单/总/package/batch+failure reserve边界；1-byte CHUNK累计header限制不误拒合法流 |
| EOF / slow client | SHUT_WR成功；COMMIT无half-close、尾随byte、第二COMMIT失败；idle/header/总180s/EOF5s分别覆盖，持续滴字节不能续期，排队计入budget |
| disconnect / ACK loss | 各输入阶段断开无该次partial rows；完整EOF后断开允许已提交；ACK部分写/timeout/crash不回滚原package/outbox、不触发重fetch |
| duplicate submission | 同连接重复COMMIT拒绝；新连接同pin合法replay返回同receipt且row/event计数不变；错digest/owner/source/fence拒绝；lease过期不冒充历史receipt |
| concurrent clients / pressure | 超1 active和4 waiting拒绝/有界等待；两端独立接收；相同job并发无重复package；FD/线程/缓冲/staging计数有界，故障后释放槽位 |
| restart / stale socket | 正常停机、accept/read/事务前中后真实子进程退出；重开SQLite完整性；活socket不unlink，证据不足stale拒绝，可信stale可重建，inode替换不误删 |
| symlink/path attacks | 祖先/leaf dangling symlink、非socket、可写父目录、sidecar/marker/lock替换、请求任意路径拒绝；项目fixture不能替代真实生产父目录证明 |
| runner unauthorized | 实际内核UID与测试允许UID不符时拒绝；注入runner ACL/组/可写release拒启动；不因当前测试用户就是runner而启用生产配置 |
| commit boundary cannot be bypassed | 伪trusted_job/claim/decision、fixture importer替换、直接旧seal/seal_failure/replay对protected Store失败；真实socket路径在EOF后撤销/rotate/cancel/fence/lease变化仍由A拒绝 |
| lock ordering | 用进程barrier证明read/EOF/ACK时无guard/DB锁；COMMIT时guard持有；无Store→registry逆序；锁等待超时后无后台提交；共享guard可重入 |
| Gateway | 内核Peer→issuer/bot/audience→IdentityLedger→Bridge完整链；actor伪造、过期/重放冲突、owner/version错误、尾随请求不dispatch；响应丢失不重复Application动作 |
| audit / cleanup | 敏感哨兵不出现在日志/ERROR；拒绝洪泛有界；未知residue保留且阻止admission，无mtime盲删/误删活动staging |
| regression | A最终授权/原子outbox、原streaming/package、identity/scope、图片与formal gate全量回归；报告真实通过/失败/跳过数及Python版本 |

ACK loss后过期/revoke的完整receipt恢复验收归C；B必须验证durable事实保留与不误判，不用fake lookup宣称C完成。最大合法负载RSS/锁/取消/磁盘与完整重启压力演练归Step2.5。

## Production Blockers

1. **P1 — 独立Receiver UID直接只读protected registry vs 0700 state root：未解决，Production Deployment Blocker。** RegistryReader的mode=ro/query_only不是OS目录访问授权。Controller访问Receiver-owned acquisition亦有同类问题。离线合成接线不需要突破此限制；不得world-readable chmod、复制授权缓存、Guard RPC、合并UID或赋runner服务身份绕过。管理员须提出经Contract审查认可的最小能力交付；若改精确mode/拓扑先Addendum。
2. **P2 — 单socket_root/0750与两个独立socket owner的创建、group穿越、重启清理：未验收。** 不能假设服务都有目录写权；预配置/FD交付方案须另审，当前不实施。
3. **P3 — production capability roots/不可写release/真实ACL与服务UID隔离尚未证明。** 当前Store PROJECT路径与注入verifier不足；B必须补离线机制测试，实际部署权限另验收。
4. **P4 — C完整receipt/reconciler/recovery与重启可信时钟未完成。** ACK丢失不确定性不得被B隐藏。
5. **P5 — Step2.3/2.4获取/下载/spool、实际Gateway认证来源与Step2.5资源、取消/锁延迟、磁盘和恢复验收未完成。** `max_rss_bytes`、`max_sqlite_transaction_ms`、`max_guard_wait_ms`、`max_store_lock_wait_ms`、`max_cancel_latency_ms`、`max_temporary_disk_bytes`没有批准实测值前不能生产GO。

## Contract Addendum Decision

**B离线实现不需要修改冻结Contract；本轮不提出已获批准的契约变更。** BEGIN解释为既有HELLO，GID额外约束/队列/timeout是保守transport配置，Gateway独立framing承接此前Review，不改变package/receipt/wire字段或授权来源。

已识别的源码差距（单次HELLO resolver、真实peer、bounded锁等待、path capability）是冻结要求尚待接线，不是通过改Contract消除的冲突。A的final boundary、RegistryReader、outbox与本B方案没有需要变更的语义冲突。

P1/P2是实际生产配置张力，尚无可验证解决方案，**不得据此宣称无需生产Addendum**。若后续方案必须改变0700/0750、服务独立UID、RuntimePaths拓扑/字段、registry直接只读/guard顺序等冻结要求，应停止该实现，单独提交Contract Addendum，包含原条款、具体差异、最小权限/撤销线性化证明、迁移与负向验收；审查通过前保持Production NO-GO。不能在B中悄悄采用ACL、FD或特权代理作为已批准例外。

## Step2.2-B GO/NO-GO

- **设计推进：GO，仅允许后续项目内离线实现。** 默认拒绝、原wire、A提交边界、明确C范围和生产blocker均已明确。
- **B完成验收：目前未执行。** 只有真实Linux socket/SQLite/Python3.10矩阵与既有回归通过、限额/锁序/旁路拒绝有证据、变更严格在范围内、跳过项不掩盖B安全要求，才可标记“B离线完成GO”。关键测试失败、绕过A、未知身份fail-open、协议/模式静默变更、无界输入/网络持锁均为B NO-GO。
- **Production：仍NO-GO。** B离线完成也不能消除P1/P2，更不能代替C/Step2.5验收。真实跨UID证据缺失须明确留作生产阻断，而非以同UID fixture通过替代。

本轮交付：仅新增本文；静态审查上述冻结文档及相关实现/测试名称，未运行Python单元/集成测试、未部署或联网、未访问真实凭据、未commit/push。Git基线核验因当前目录不是可用仓库而未完成；后续B编码、C对账和生产阻断解除仍待独立任务。
