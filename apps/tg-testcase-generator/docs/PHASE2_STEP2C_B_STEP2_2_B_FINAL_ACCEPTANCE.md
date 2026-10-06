# Step 2.2-B Final Acceptance Matrix — crash / barrier / pressure

## 最终权威验收与关闭（2026-10-06）

本节为当前结论，依据用户提供的服务器最终独立复验实际结果；本次仅做文档关闭，未重新执行服务器测试或 compile。下方 Codex sandbox、root 服务器失败与 fixture 修订记录全部保留为历史证据，其旧 NO-GO、待复验及根因待确认状态均已 **superseded**，不得作为当前 Step2.2-B 状态。

| 当前范围 | 最终状态 |
|---|---|
| Capability-Root Contract §9 offline blocker | **CLOSED** |
| Step2.2-B offline acceptance | **GO / CLOSED** |
| Phase 2 Step 2C-B Step 2.2-B | **CLOSED** |
| Production | **NO-GO** |
| Step2.2-C | **remains unimplemented** |

### codex-runner 最终权威服务器结果

执行身份：`uid=998(codex-runner) gid=998(codex-runner)`。同一代码使用真实 AF_UNIX + real SO_PEERCRED 完成独立复验。

| 验证 | 用户提供的实际结果 |
|---|---|
| Pressure targeted | 3 tests；Ran 3 tests in 10.416s；OK；TARGET_RC=0；RUNUSER_RC=0 |
| Full suite | Ran 397 tests in 60.717s；OK；FULL_RC=0；COMPILE_RC=0；RUNUSER_RC=0 |
| 最终统计 | 0 failures；0 errors；0 skipped |

Final acceptance matrix 源码已由服务器侧人工核对；以下计数来自用户提供的核对证据，不是本轮重新执行测试的结果。25 项包含于全量 397 项中，不另造矩阵独立运行耗时或退出码。

| Acceptance 类别 | 数量 | 覆盖 |
|---|---:|---|
| Crash | 4 | 事务前、BLOB 后、outbox 后 COMMIT 前、COMMIT 后 |
| Barrier | 8 | revoke/rotate/cancel/fence × before/after commit，动态生成 |
| Socket Boundary | 10 | read/eof/after_eof/ack/partial_ack × normal/kill，动态生成 |
| Pressure | 3 | admission/overflow/recovery、等待过期、两端独立 |
| 合计 | **25** | 最终全量服务器复验通过 |

### 历史证据的归属与取代关系

- **Codex sandbox 历史结果（superseded）**：初次矩阵 25 tests / 6.500s，12 passed、13 errors；初次全量 397 tests / 43.845s，373 passed、24 errors；fixture 修订后 Pressure 3 tests / 0.003s，3 errors，全量 397 tests / 46.496s，373 passed、24 errors。具体环境拒绝及原始统计保留于下方；它们不是服务器最终结果，也不再阻断此次离线关闭。
- **root 服务器历史失败（superseded，原因已确认）**：历史 397 tests / 55.455s 中三项 Pressure failures 已确认是 fixture/执行身份问题。root uid=0 被 frozen PeerPolicy 正确拒绝，handler 不应进入，旧 fixture 对 handler 进入的假设不成立。这不是 sandbox 失败，也不要求放宽安全规则。下方“服务器身份或根因待确认”的表述仅反映当时证据状态，现已被该确认取代。
- **codex-runner 最终权威结果**：新版 fixture 明确要求非 root；没有放宽 root rejection，没有 mock SO_PEERCRED，没有延长 wait 掩盖问题。同一代码在 uid=998/gid=998 下 Pressure 3/3、全量 397/397 通过，取代旧离线 NO-GO 与待复验状态。

### Frozen hash 与 publication artifacts

服务器再次验证的 SHA-256 如下，本次文档关闭也已在当前 workspace 只读复核，两者一致：

| Frozen 文件 | SHA-256 |
|---|---|
| PHASE2_STEP2C_B_STEP2_DESIGN.md | `eb6e6e953e49d7c7f4258b14109df91636f7b0355b1e3b04835e746cc66d42df` |
| PHASE2_STEP2C_B_STEP2_CONTRACT.md | `0e1a798bc2326a9dfd09b72e0572cd8891e09fc3383ee5f95666e8d29b150299` |

用户提供的 Task `20261005-122935-452FEA` After Snapshot artifact check：无 `.capability-tests.log`、无 `*.log`、无 `.test-runtime`、无 pycache、无 `*.pyc`。这是服务器快照证据。当前 Codex workspace 仍有历史日志、`.test-runtime`、`__pycache__` 与 `*.pyc`，不能把服务器干净快照描述成本地现状；本次不修改或清理这些既有文件，不新增日志、缓存或临时文件。

当前 workspace 无可用 Git 元数据（`git status` 返回 `not a git repository`），无法提供 Git diff 或核验提交身份。本次仅修改本文和现有 Step2.2-B DELIVERY 的状态链接；未执行 Git commit/push。

### 关闭边界与剩余生产阻断

此次仅关闭 Step2.2-B offline acceptance，不新增功能，不修改 src、tests、frozen Design/Contract/Design Review、schema、wire、Capability-Root、部署或其他代码。Production 保持 **NO-GO**；Step2.2-C remains unimplemented。

production UID/socket/provisioning/topology 阻断继续保留，包括跨 UID registry/acquisition 访问、single socket_root 0750 与独立 owner、provisioner/FD/ACL/service topology。C 阶段 receipt/reconciler、跨重启可信时钟与恢复，以及既有后续阶段生产验收义务，不因本次离线关闭而解除。

## 历史证据归档（以下状态已 superseded）

以下保留原始证据和当时判断；其中命令、日志路径及文件变更描述属于历史轮次，不是本次执行或修改记录。

日期：2026-10-05。范围：当前 workspace，项目内合成 fixture；未修改实现、安全模型、冻结规范、schema、wire、receipt 或 Agent。

**Capability-Root Contract §9 offline blocker：CLOSED。** 接受用户提供的独立服务器证据：Python 3.10.12，372 tests / 41.048s，OK，TEST_RC=0，COMPILE_RC=0。历史 11 项 AF_UNIX errors 已由该服务器运行确认通过；不再作为 Capability-Root blocker。此证据不覆盖本轮新增测试。旧 DELIVERY 中的停止结论及路径能力缺口描述属于历史，当前以 Capability-Root 专项交付及本记录为准。

**历史状态（superseded）：服务器 397 项中 3 项 Pressure failures；本次 fixture 修订仍待服务器复验，offline Step2.2-B NO-GO；Production NO-GO。** 本轮未发现需要改变 Capability-Root 的真实 blocker。

## 新增可执行矩阵

文件：`tests/test_step22b_acceptance.py`。所有 fork 发生在父进程未持 guard/Store 锁时；子进程有截止时间、退出码检查和强制回收。同步使用 multiprocessing Pipe，不以固定 sleep 推断提交时序。网络测试不将 syscall 拒绝转成 skip。

| 测试 | 证据及判据 | 本地状态 |
|---|---|---|
| CrashTests：事务前、BLOB 后、outbox 后 COMMIT 前、COMMIT 后，4 项 | 真实子进程 os._exit；重新打开 protected Store；integrity_check / foreign_key_check；package/artifact/manifest/outbox 全零或全一；fetching/sealed；artifact bytes；提交后 replay 不改行 | 4 通过 |
| BarrierTests：revoke / rotate / cancel / fence × 两种先后顺序，8 项 | 变更先完成则拒绝 seal；final finish 已写 outbox、尚未 COMMIT 时另一进程独立打开 guard 并获得 EWOULDBLOCK；父进程提交释放后变更才完成；持久提交保留 | 8 通过 |
| SocketBoundaryTests：PACKAGE 后、等待 EOF、完整 EOF 后、ACK 前、部分 ACK × 锁探测/kill，10 项 | 真实 socket 与 ReceiverService；暂停时父进程取得 guard、registry BEGIN IMMEDIATE、Store BEGIN IMMEDIATE；SIGKILL 后重开 SQLite；ACK kill 后完整 wire replay，四表逐行不变 | sandbox SO_PEERCRED 拒绝，待服务器 |
| PressureTests：1 active + 4 waiting + overflow | 真实 listener/kernel peer；第六条关闭，等待不调用 handler；内核 buffer 上限；单 worker identity；handler 异常后排队及新请求成功；服务端连接关闭、unfinished_tasks 清零 | sandbox bind EPERM，待服务器 |
| PressureTests：等待过期 | active 暂停时四条等待连接按实际 10 秒过期并关闭；不进入 handler；新连接恢复 | sandbox bind EPERM，待服务器 |
| PressureTests：两端独立 | Receiver 活动槽暂停时独立 Gateway endpoint 完成响应 | sandbox bind EPERM，待服务器 |

Barrier fence 使用测试内 guard 下 acquisition fence 更新，证明最终边界读取及排序，不声称实现新的生产 fencing API。Crash hooks 只暂停/终止原调用，不替代 SQLite COMMIT、授权或锁。确定性 COMMIT 前后测试不宣称捕获内核 COMMIT 指令中间；真实不确定结果仍以重开数据库事实判定。网络压力 handler 用于直接验证 Endpoint admission/资源生命周期，不代表最大合法 package RSS 验收。

## 执行与复验

解释器 Python 3.10.12。新增矩阵：25 tests / 6.500s，12 passed、13 errors、0 failures、0 skipped。13 errors 为 3 个 bind EPERM 和 10 个 kernel_peer fail-closed；未执行到对应网络断言，不声明通过。compileall 返回 0。

最终 workspace 全量回归：**397 tests / 43.845s，373 passed、24 errors、0 failures、0 skipped，TEST_RC=1；COMPILE_RC=0。** 24 errors 为历史 11 项与新增 13 项 AF_UNIX 环境拒绝；历史 11 项已有用户提供的服务器通过证据，新增 13 项尚无。未发现非 socket 测试失败。

运行日志位于项目内 `.test-runtime/step22b-acceptance-tests.log`、`.test-runtime/step22b-final-tests.log`。服务器请从项目根执行：

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src:tests .venv/bin/python -m unittest test_step22b_acceptance -v
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -v
PYTHONPYCACHEPREFIX="$PWD/.test-runtime/step22b-final-compile" .venv/bin/python -m compileall -q src tests
```

需要保留退出码与完整 passed/errors/failures/skipped 统计；旧快照 372 项通过不能代替新增 25 项验证。当前 workspace 无可用 Git 仓库，无法给出提交身份或 git diff。

## 剩余边界

本次补的是 crash / barrier / pressure 测试，不把整个冻结 Review 的其他矩阵自动标记完成。最大合法负载 RSS/磁盘/取消延迟仍归 Step2.5；完整 receipt/reconciler、跨重启可信时钟/恢复归 C；未知 crash staging 保留与 admission 拒绝沿用现有机制，没有增加自动清理或 stale socket 重建。生产跨 UID registry/acquisition 访问、单 socket_root 0750 与独立 owner、provisioner/FD/ACL/service topology 等阻断保持。

新增文件仅测试文件和本文；历史交付加当前状态链接。测试运行产生项目内日志及 compile 缓存；无生产配置、部署、联网、上传或 push。


## Pressure Acceptance Fix（2026-10-05，历史记录，superseded）

### 服务器证据及根因边界

用户提供 Task `20261005-105142-8AD664 After Snapshot`：Python 3.10.12，
397 tests / 55.455s，FAILED (failures=3)，其余 394 项通过，COMPILE_RC=0。
仅 `test_active_four_waiting_overflow_and_recovery`、
`test_waiting_expiry_and_recovery` 的 active.wait(3)，以及
`test_slow_receiver_does_not_block_gateway` 的 entered.wait(3) 失败。
这些服务器 failures **不归因于 sandbox**。上文“新增 13 项尚无服务器证据”是修订前历史记录，
本次服务器结果已经覆盖它们，其中仅这三项未通过。

分类：**A 类 fixture 缺陷已由代码确认；服务器三个 failure 的具体触发条件尚待身份证据确认；
目前没有证明 B 类实现缺陷。不能把候选原因写成已经复现的服务器根因。**
原 fixture 使用 `PeerPolicy(os.getuid(), os.getgid())`，默认当前进程必能认证。
但 PeerPolicy 必须无条件拒绝 UID 0，SO_PEERCRED 使用连接建立时的有效 UID/GID。
因此 root 进程必然被拒绝；real/effective UID/GID 不一致也可能导致拒绝。
这两条路径都会在入队前关闭连接，handler 永远不进入，而旧测试仅报告 event 超时。
提供的服务器摘要没有 UID/GID 或 admission 记录，无法据此断言服务器确实以 root 运行。
已请求补充这些信息。没有切换身份、提权或伪造 peer，也没有允许 root 通过。

### 真实执行路径审查

- accept 后立即读取真实 kernel_peer → PeerPolicy.authorize → configure_socket →
  waiting.put_nowait。认证失败、buffer 配置失败或 queue.Full 均关闭连接；未读取应用字节。
- Endpoint 使用每实例 Queue(4)、stop event 和一个 worker，没有额外 semaphore 或共享 active slot。
  worker.get 移出当前 active 项，留出四个等待位；第五个等待连接关闭。
- accept 循环清理等待超过 10 秒的连接并递减 unfinished_tasks；worker 也检查等待年龄。
  handler 正常或异常返回均在 finally close/task_done，随后继续取下一项。
- 首次 active 未进入时尚未创建压力等待连接，因此不能由这些测试后续的 overflow/expiry 导致。
  原测试已在 listen 后启动 serve，并等待 active 后才填队列；没有发现启动顺序必然失效的证据。
- Receiver/Gateway 的 Endpoint 队列、worker、stop 相互独立；local_budget 为 ContextVar，
  服务 reader/deadline 每请求独立。Pressure 使用直接 handler，不进入 ReceiverService/
  GatewayTransport 的 local_budget，因此其提前耗尽不能解释这三个首次 event 超时。
- 未发现 Capability-Root、Store 或 protected staging 是根因；它们保持不变。

### 最小修订及回归判据

仅修改 `tests/test_step22b_acceptance.py` 和本文，无生产实现变更。
fixture 使用有效 UID/GID；在启动 listener 前用真实 AF_UNIX socketpair、SO_PEERCRED、
原 PeerPolicy 和 configure_socket 预检。root 运行明确 FAIL，要求测试由现有非 root
测试账号执行；不 skip、不自动改变身份、不降级安全规则。

每个 Endpoint 使用只记录结果并调用原 authorize 的 policy 子类，记录真实 peer 的通过/拒绝。
两端 accept 线程异常均被收集；保留 3 秒有界条件等待，超时列出线程状态、认证记录、
queue size、unfinished_tasks 和有效 UID/GID，避免只看到 event 未触发。
没有 patch kernel_peer、fake credential、替换 socket 或增加等待上限。

压力回归仍验证 active 确实进入、四条真实认证等待、第五条关闭、单 worker、buffer 上限，
active handler 抛出异常后四条排队及新连接成功、六次 handler 调用、连接关闭和计数归零。
过期回归新增：active 仍阻塞时四条等待实际经过 10 秒关闭，unfinished_tasks 仅余 active；
立即重新填满四位并再次验证 overflow，然后释放 active，四条新等待及后续连接成功。
独立进度回归新增：Gateway 响应时 Receiver handler 尚未退出，两个 Endpoint 队列及 stop
不共享；完成后两端计数归零并回收线程。

### 本地验证与最终判定

解释器 Python 3.10.12；当前进程 UID/GID=998/998。

| 检查 | 本次结果 |
|---|---|
| targeted PressureTests（修订后） | 3 tests / 0.003s；0 passed、3 errors、0 failures、0 skipped；RC=1 |
| full suite | 397 tests / 46.496s；373 passed、24 errors、0 failures、0 skipped；RC=1 |
| compileall src tests | RC=0 |

本地原版 targeted 先复现了 3 个 bind EPERM（3 tests / 0.006s）；修订后这三项在真实
socketpair 的 kernel_peer 预检即抛出 TransportRejected。保留错误原貌如下：

```text
原版：listener.bind(path)
PermissionError: [Errno 1] Operation not permitted
修订版：PressureTests.setUp -> preflight -> kernel_peer(server)
unix_transport.py:71: raise TransportRejected() from None
TransportRejected: transport_rejected
Ran 3 tests in 0.003s
FAILED (errors=3)
Ran 397 tests in 46.496s
FAILED (errors=24)
```

全量 24 errors：PressureTests 3、SocketBoundaryTests 10、PeerTests 1 在 kernel_peer
失败；PathTests 3 在 socket bind 失败；ReceiverTests 7 在 socket sendall 失败。
这仅是当前 sandbox 的本地验证限制，不能解释服务器的三项 event timeout。
本地没有执行成功的 Pressure admission/expiry 证据，不能标记回归通过。
本轮临时测试输出、编译缓存及比对备份已删除；已有的历史 .test-runtime/log/pycache
不是本轮正式改动，未作为交付文件。

服务器复验须在实际非 root 测试账号下执行以下命令，记录 real/effective UID/GID、
完整结果与退出码。若仍失败，保留新增 admission/线程诊断，继续定位；不延长等待或跳过。

```sh
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -c 'import os; print("uid/euid/gid/egid", os.getuid(), os.geteuid(), os.getgid(), os.getegid())'
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src:tests .venv/bin/python -m unittest test_step22b_acceptance.PressureTests -v
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -v
PYTHONPYCACHEPREFIX="$PWD/.pressure-compile-cache" .venv/bin/python -m compileall -q src tests
```

**历史判定（superseded）：服务器独立复验仍必需；offline Step2.2-B NO-GO，未 CLOSED；Production 必须继续 NO-GO。**
Capability-Root Contract §9 保持 CLOSED；不实现 Step2.2-C，不修改冻结规范、部署、wire、schema
或其他受限范围。本次未 commit/push；workspace 没有可用 Git 元数据。
