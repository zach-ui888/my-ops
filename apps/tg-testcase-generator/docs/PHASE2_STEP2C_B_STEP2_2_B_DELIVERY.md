# Phase 2 Step 2C-B Step 2.2-B Delivery

> 当前状态更新（2026-10-06）：Capability-Root Contract §9 offline blocker = CLOSED；Step2.2-B offline acceptance = GO / CLOSED；Phase 2 Step 2C-B Step 2.2-B = CLOSED。codex-runner 最终权威服务器复验 Pressure 3/3、全量 397/397 通过，详见 [Final Acceptance](PHASE2_STEP2C_B_STEP2_2_B_FINAL_ACCEPTANCE.md)。以下旧 NO-GO、停止及待补齐记录均为历史状态，已 superseded。Production = NO-GO；Step2.2-C remains unimplemented；production UID/socket/provisioning/topology 及 C 阶段 receipt/reconciler 等生产阻断继续保留。

## 2026-10-05 Final Offline Validation：Contract blocker 停止记录

**Step2.2-B offline implementation/validation：NO-GO；Production：NO-GO。** 本次依据用户明确指定的 capability-root Contract blocker 停止条件，仅核对冻结要求与源码、补充本记录；未扩大实现，未修改测试或冻结文档。下方 2026-10-04 记录保留为历史，原 11 errors 的环境归因以本节更新为准。

用户提供的 Task `20261004-123136-326C2B` After Snapshot 服务器独立复验：项目 venv、Python 3.10.12、`Ran 350 tests in 39.910s OK`、`TEST_RC=0`、`COMPILE_RC=0`。接受此证据：原 11 项 errors 已确认是 Codex sandbox AF_UNIX syscall 限制，不是代码测试失败。该结果是用户提供的上一快照证据，不是本次执行结果，也不能覆盖尚未编写的矩阵测试。所提供摘要未单列 skipped 数，本次不推造该数字。

### 具体停止依据

- 冻结 [CONTRACT §9](PHASE2_STEP2C_B_STEP2_CONTRACT.md) 第 370 行：**“Step 2.2 SHALL 解除 Store/storage 对 PROJECT 的硬耦合，改为显式 capability roots/descriptor-relative/no-follow 的允许根，不是删除所有路径检查”**。第 429 行再次将 Store/storage 耦合 PROJECT 的差距归 Step2.2。
- Contract 本身使用 Step2.2 总范围；B 的具体分工由冻结 [B Design Review](PHASE2_STEP2C_B_STEP2_2_B_DESIGN_REVIEW.md) 细化：第 179 行将 scoped capability root、安全打开/sidecar/staging 列入 B；第 186 行把 path capability 接线列入离线实施顺序；第 219 行明确“当前Store PROJECT路径与注入verifier不足；B必须补离线机制测试”；第 227 行将 path capability 列为冻结要求尚待接线。结合这些条款，不能自行将全部 scoped root 接线归为仅 Production 工作。
- 当前 `src/tg_testcase/store.py:22` 仍为 `self.root = inside_project(root)`；`src/tg_testcase/storage.py:8–16` 将允许根绑定到代码推导的 PROJECT；其 descriptor-relative 清理同样从 PROJECT 开始。`LocalSocketCapability` 也调用 inside_project，它提供本地 socket 目录能力，不能替代 Store/storage scoped root 接线。
- 现有检查可支持项目内 fixture，但这不等于满足上述更广的离线路径能力要求。上一交付“B 尚未完成的工作”第 3 项已明确承认此缺口。这里报告的是既有契约未完成与本轮范围限制之间的 blocker，不要求修改 Contract，也不授权解决生产拓扑。

因此按用户“停止并报告具体 Contract blocker，不得自行改变 Contract”的指令停止新增实现与验收测试。需要后续明确该冻结要求的 B 范围处置或另行授权符合冻结规范的离线路径能力工作；本次不放宽检查、不实现生产 capability-root、不自行延期冻结义务。

### 本次执行、剩余测试与外部复验

本次测试执行数 **0**（passed 0 / failed 0 / skipped 0；未运行，不是通过）；compile **未执行**。只做静态文件核对；Git status 返回 `not a git repository`，不能核验快照身份。未 commit/push、未联网、未访问凭据。

原有真实 Linux AF_UNIX 测试的精确外部命令（在项目根目录执行；当前用户提供的上一快照复验已通过，并非尚未解决的代码错误）：

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src:tests .venv/bin/python -m unittest -v \
  test_step22b_transport.PeerTests.test_real_kernel_peer \
  test_step22b_transport.ReceiverTests \
  test_step22b_paths.PathTests.test_lifecycle \
  test_step22b_paths.PathTests.test_unknown_stale_socket_is_retained \
  test_step22b_paths.PathTests.test_replaced_node_is_not_deleted
```

ReceiverTests 的七项名称：`test_half_close_commit_and_full_replay`、`test_trailing_byte_never_commits`、`test_commit_without_half_close_times_out`、`test_truncated_input_never_commits`、`test_ack_loss_preserves_outbox`、`test_unknown_residue_blocks_and_is_retained`、`test_no_db_locks_during_eof_or_ack`。未来新增真实 socket 测试也必须在允许 AF_UNIX 的 Linux 环境执行；不得把 sandbox EPERM 改成 skip 或 mock PASS。

后续完成范围处置及测试补齐后，全量回归与项目内 compile 命令：

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -v
PYTHONPYCACHEPREFIX="$PWD/.test-runtime/step22b-final-compile" .venv/bin/python -m compileall -q src tests
```

本轮仍未补齐：真实进程 crash 各窗口/SQLite 恢复、multiprocessing final COMMIT 与 revoke/rotate/cancel/fence barrier、真实 read/EOF/ACK 锁序证明、1 active + 4 waiting 压力与资源释放、完整 framing/budget 边界、ACK-loss artifact/outbox 全流 replay 证据、完整 lifecycle/path 与敏感哨兵矩阵。现有部分测试和历史 350 项通过不能替代这些缺项。

Production blockers 保持：独立 Receiver UID → registry_state_root 0700；Controller → Receiver-owned acquisition 跨 UID；single socket_root 0750 + independent socket owners；production provisioner/FD delivery/ACL topology、受保护 release 与服务隔离、service/systemd/deploy；Step2.2-C receipt/reconciler 与恢复；Step2.3/2.4 fetch/downloader/spool；Step2.5 资源、锁/取消延迟与恢复验收。均未在本次解决或放宽。

本次文件变更仅本文；未创建或修改实现、测试、schema/migrations、requirements、wire/receipt 或 Agent。

日期：2026-10-04。范围：项目内离线实现，未部署、未联网、未访问真实凭据、未修改 Agent、requirements、schema、migration、Application wire 或冻结规范。

**结论：实现与验证尚未达到 B 完成验收条件，B NO-GO；Production 继续 NO-GO。不得将本次代码或注入 I/O 测试结果当作可部署服务。**

## 已实现的接线

- `unix_transport.py`：AF_UNIX/SOCK_STREAM 的原生 `@iII` SO_PEERCRED 提取；默认拒绝的精确 UID/primary GID 策略；基于 RuntimeOwners 的端点角色策略显式排除 runner/root；无请求字段、supplementary group 或 PID 查询 fallback。
- 固定 `receiver.sock` / `gateway.sock` 的本地目录管理能力：项目内 descriptor/no-follow 目录检查、owner/group/0750、ACL 保守拒绝、107 字节限制；创建使用 umask 0077，仅对本次创建且确认的 socket 设置 0660。既有节点全部保留并拒绝，包括无法证明归属的 stale socket；只按本实例 device/inode 清理。此接口名为 `LocalSocketCapability`，不提供生产 provisioner 或生产权限证明。
- `Endpoint`：单工作线程、最多四个已认证等待连接；认证先于排队/读取；等待超时清理；显式 socket buffer 上限。输入从 accept 起计 180 秒，排队计入输入预算；header 总计 10 秒、idle 10 秒、EOF 5 秒；写响应独立 5 秒。无逐连接线程。
- `receiver_service.py`：HELLO 由原 receiver 单次严格解析，通过 job ID 构造实际 `ProtectedReceiverAcquisition` / `RegistryReader`；保留 A 的最终授权、原子封存与 outbox。没有另一套 seal/authorize，也没有新增 receipt lookup。
- receiver 保留原 HELLO/PACKAGE/ARTIFACT/CHUNK/END/COMMIT 和三字段 receipt。所有读取共用 bounded reader；确认实际 EOF 后才封存。按 package 声明收紧累计字节和 frame 数上限，保留合法单字节 CHUNK。未知 staging residue 保留并拒绝本次 submission，检查 staging 可用空间，不按 mtime 删除。
- `gateway_transport.py`：独立 uint32 大端 framing、64 MiB 请求/响应上限、严格 UTF-8、真实 EOF 后才调用既有 ControllerBridge/IdentityLedger。响应超限发送固定 storage_error；不撤销已完成的动作。`BoundedGatewayRegistry` 保持原 writer 事务语义，只收紧 SQLite busy timeout；不修改 JobRegistry schema/状态机。
- Guard / Store 的 flock 改为非阻塞重试和 monotonic 最多 5 秒等待；Receiver registry/Store SQLite busy timeout 受同一剩余预算约束。Guard 固定 inode，Store 检查根与锁 inode、no-follow 锁以及 DB/sidecar 常规文件类型。最终 A 时间检查额外检查 transport 剩余预算。
- 不新增日志输出；transport ERROR 保持固定 `seal_rejected`，Gateway framing 错误保持固定 `storage_error`。不输出原始异常或输入内容。

## 验证与环境限制

实际解释器：项目 `.venv/bin/python`，Python 3.10.12。全部运行设置 `PYTHONDONTWRITEBYTECODE=1`；fixture、SQLite、socket 路径在项目内。

- 既有回归单独执行：**322 tests，全部通过，0 skipped**。
- A 最终提交回归单独执行：**49 tests，通过**（属于上述 322 项，不重复累计）。
- 注入 I/O + 实际 protected SQLite 的 Receiver/Gateway 接线：单字节分片、相同 pin 全流重放、尾随字节/第二 COMMIT/截断、超长 header、自报身份字段、EOF 后 revoke、ACK 写失败后的 package/outbox 保留、真实 IdentityLedger/ControllerBridge 的持久幂等通过。
- 额外真实子进程检查：在注入 I/O 的 EOF 与 ACK 边界，另一个进程能够取得 AuthorizationGuard、registry 写事务和 Store 写事务。**这不是成功的真实 socket 边界测试，也不是最终 COMMIT/revoke 进程 barrier 的完整证明。**
- 路径负向和有界 flock 检查通过：普通文件/目录/FIFO/dangling symlink 保留、错误目录 mode/group、symlink 父目录、路径超长、锁等待超时及 Guard inode 替换拒绝。
- 本环境对项目内真实 Unix socket `bind`、`sendall`、`getsockopt(SO_PEERCRED)` 返回权限错误。保留这些真实测试为失败，未改为 skip、未用 mock 冒充通过、未请求提权或尝试绕过沙箱。
- 系统 `python3` 首次回归因缺少 PIL 导致导入错误；项目虚拟环境具备依赖，没有安装或更改 requirements。
- 当前目录不是可用 Git 仓库，无法独立核验发布提交或给出 Git diff。没有 commit/push。

最终全量运行结果见下方更新记录；不能只引用通过的子集宣布验收通过。

## B 尚未完成的工作

1. 在明确允许项目内 Unix socket 操作的受限测试环境重跑真实 peer、listener lifecycle、半关闭/超时、ACK 丢失和重放测试。当前受限环境无法取得这些必须证据。
2. 补齐并执行完整矩阵：真实 accept/排队压力、慢发/最大合法边界、各 crash 窗口的进程退出与 SQLite 恢复、最终 COMMIT/revoke barrier、两端并行与所有敏感哨兵路径。当前测试不是冻结 Review 全矩阵的替代品。
3. 当前只有显式本地 socket 目录能力和现有项目内 Store/SQLite 路径加固；**Store/storage 尚未完成冻结 Review 要求的通用 scoped capability-root 接线**，不能称为解除 PROJECT 耦合，也没有实现受保护 provisioner 的监听能力交付。生产入口不存在，不能把本地 fixture 能力包装成生产配置。
4. 当前策略拒绝所有既有 socket；没有提供持有受保护停机/恢复证据时的 stale 重建入口。未知 residue 保留并拒绝 submission；没有跨重启 owner/job/attempt/fence 隔离清理器。
5. 未实现 Producer 客户端的固定节点与 server peer 校验、完整历史 receipt/reconciler、跨重启可信时钟恢复。完整历史 receipt/reconciler 仍归 C；不能以本次全流 replay 代替。

因此本次是可审阅的实现进展，不是 Step2.2-B 完成声明。没有发现必须修改 frozen Contract 才能实现的语义冲突，也没有修改或默认批准 Contract Addendum。

## Production blockers（保持）

- **P1：独立 Receiver UID → registry_state_root 0700 仍不可直接获得所需访问能力。** Controller 访问 Receiver acquisition 亦有同类问题。没有放宽 mode、合并 UID、复制授权缓存或增加 Guard RPC。
- **P2：single socket_root 0750 + independent socket owners 的创建、穿越与重启清理仍未解决。** 没有改为 0770/0777，没有实现未经审查的 FD 交付/管理员代理。
- **P3：production capability/deployment topology、真实 ACL、不可写 release、服务 UID 隔离未获证明。** 同 UID 本地 fixture 不能代替这些证明。
- P4/P5：C receipt/reconciler/recovery、后续获取/下载/spool 与 Step2.5 RSS、磁盘、锁和取消延迟验收仍待完成。

上述任何需要改变冻结路径、精确 mode、独立 UID 或直接只读 registry 拓扑的方案，须单独审查 Contract Addendum；本次没有实施例外。**Production NO-GO。**

## 文件清单

新增：`src/tg_testcase/unix_transport.py`、`receiver_service.py`、`gateway_transport.py`；`tests/test_step22b_transport.py`、`tests/test_step22b_paths.py`；本文。

修改：`src/tg_testcase/receiver.py`、`authorization.py`、`registry_reader.py`、`protected_acquisition.py`、`runtime_paths.py`、`store.py`。

## 最终运行记录

命令：`PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -q`。

**350 tests / 34.087s：339 通过，11 errors，0 skipped。** 11 项错误全部在真实 Unix socket 测试：3 项 listener/stale/节点替换生命周期，1 项真实 SO_PEERCRED，7 项真实 Receiver 传输场景。`bind`/`sendall` 的错误是 `PermissionError: [Errno 1] Operation not permitted`；SO_PEERCRED 的 OS 错误由 fail-closed 提取器转换为固定 `transport_rejected`。这些错误阻断 B 验收，不视为通过。

最终源码的原有 322 项回归在这次运行中也全部通过；新增 28 项中 17 通过、11 因上述限制报错。没有将实际 syscall 错误改成跳过，也没有使用其他身份或关闭安全控制重试。
