# Phase 2 Step 2C-B Step 2.2-A Delivery

本次是离线实现，范围为 protected acquisition、最终 COMMIT 授权、durable outbox、additive migration 和确定性安全测试。完整阅读 DESIGN、冻结 CONTRACT、Step 2.1 DELIVERY、Step 2.2 DESIGN REVIEW；本次范围内未发现 Review 与 CONTRACT 的冲突。冻结 CONTRACT 优先，未修改四份依据文档。

用户消息在“adapter 必须：”处截断；已请求补充，以下交付对应当前可见的 A–F 目标。没有推定缺失正文的额外要求。

## 已实现

最终公共内核仍在 `OfflineAcquisition._commit_staged`。protected importer 在全部 package/artifact 输入和校验、receiver COMMIT/真实 EOF 完成后，复验并固定本地 staging handles，再进入：

`AuthorizationGuard → registry 只读一致快照并关闭事务 → Store lock → BEGIN IMMEDIATE → acquisition 权威状态 → authorize_commit → package/artifacts/sealed/manifest/outbox → 最终不变量与 deadline/lease 检查 → COMMIT → Store 解锁 → Guard 解锁`。

- `RegistryReader` 使用 SQLite `mode=ro`、`query_only`、短 `BEGIN`；不执行 migration/DML/BEGIN IMMEDIATE。复用原有 job/semantic key 和 policy/head/history/verification 校验代码。受 Guard 保护的 snapshot 只在本次持锁期有效，进入 acquisition 后不再访问 registry DB。
- 最终授权继续使用现有 `authorize_commit`。job/pin/state/cancel、principal/grant/root/type/workspace、epoch/digest、credential generation/enabled/deleted、verification、registry attempt/fence/worker/lease/deadline 均来自权威 registry。
- 同一 acquisition 写事务检查 task 列/payload 一致性、owner/cancel、当前 source 与固定 source_inputs 的 task/batch/revision/root/kind、batch membership/state、真实 claim tuple/status/lease、package/source 唯一性与 batch failure reserve。普通 task.version 增加不与 job.task_expected_version 做 seal CAS。
- BLOB/manifest/outbox 写入后再读 acquisition 状态，验证 package、artifact 元数据/长度、manifest、event、总预算和 decision，最后检查 wall/monotonic deadline 与两边 lease 的较早值。最后检查之后直接 SQLite COMMIT；不承诺中断已启动的 COMMIT。
- `ProtectedAcquisitionAdapter` 提供真实 inspect/claim/lookup/transition/heartbeat，要求外层共享 Guard；秒值向下转换毫秒。lease 使用锁内新鲜时间校验绝对上限，等待 Store 锁不会延长传入截止。拒绝旧 revision UPSERT、sealed 重 claim、失效 worker/attempt/fence。registry heartbeat 额外检查当前 source/owner。
- `receive()` 保留受保护 importer 和显式 staging root，早期重新读取可信 job/claim，拒绝伪造 trusted_job。早期检查不替代最终事务复核。wire v1 和固定 `seal_rejected` 不变。
- protected Store 写入 `.protected-acquisition` 固定模式标记；重开 Store 或先前创建的 fixture Store 都不能因默认参数退回旧 seal。旧 seal/seal_stream/seal_failure/replay 和原始 claim mutation 不能写 protected Store；缺失/伪造 commit context 被公共内核拒绝。默认未标记 Store 保持离线 fixture 兼容。
- failed package 必须先经过现有 registry pin，再经正常 protected seal；旧随机 `seal_failure` 在 protected 模式拒绝。授权撤销不能转成 failure package 绕过。wire replay 验证当前授权、有效 sealed claim、原 pin 和原 outbox，不重复写事件。

这些是可信 Python 组合层能力，不能隔离同一服务中任意恶意 Python/SQL；实际 UID、release/DB/锁目录保护仍是部署前提。模式标记不替代 RuntimePaths/ownership 验证，也不宣称已提供可部署的生产启动入口。

## 最小迁移与持久事实

acquisition schema v3 → v4 仅新增 `acquisition_commit_outbox` 和其不可变触发器。16 字段遵循 CONTRACT §5.3；event_id PK、job_id/package_id UNIQUE、source tuple UNIQUE、正整数/时间约束、package FK，Store 连接开启 foreign_keys。未改变 source_packages 列、Source Package schema、receiver wire 或图片/gate 规则；registry schema 仍为 v1。

稳定 event_id：`acq1-` + SHA256(C(["acquisition-commit-v1", job_id, task_id, batch_id, source_id, revision, package_id, package_digest]))。实际 attempt/fence/epoch/generation 来自可信绑定，时间为事务内标记，非 COMMIT 返回时刻。历史 package 不补造未知授权事件。

package、artifacts、sealed claim、适用时的 batch manifest、outbox 在同一 SQLite 事务 commit/rollback。Registry 更新失败不改变已提交事实。测试读取 durable event 并调用既有 `consume_outbox` 验证失败后重试和重复消费幂等；本次没有增加后台 reconciler 或对外 receipt 查询。

## 文件清单

新增：

- `src/tg_testcase/registry_reader.py`
- `src/tg_testcase/acquisition_adapter.py`
- `src/tg_testcase/protected_acquisition.py`
- `tests/test_step22a_commit.py`
- 本文档。

修改：

- `src/tg_testcase/acquisition.py`：公共最终提交内核及 protected bypass rejection。
- `src/tg_testcase/authorization.py`：更新已接线接口说明，保留原授权逻辑。
- `src/tg_testcase/job_registry.py`：heartbeat 当前 source 复核。
- `src/tg_testcase/receiver.py`：保留 protected importer、权威 HELLO/job/claim 比对。
- `src/tg_testcase/migrations.py`：v4 additive outbox。
- `src/tg_testcase/store.py`：持久 protected 模式、FK。
- `tests/test_application.py`、`tests/test_processor.py`、`tests/test_notion_package.py`：迁移目标版本更新为 4，合成降级 fixture 先移除新增 outbox，保留原回滚断言。

测试日志留在项目 `.test-runtime/step22a-*.log`，测试 DB/staging 全部使用项目内临时目录和合成内容。

## 验证

最终全量回归：**322 项通过，0 failures、0 errors、0 skipped**（37.333 秒），其中本次新增 49 项。完整结果：`.test-runtime/step22a-full-tests.log`。早期独立运行日志为当时的测试集合；最终新增测试均已纳入上述全量运行。

使用项目已有 `.venv/bin/python`（Python 3.10.12），无网络安装：

```sh
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p 'test_step22a_commit.py'
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -v
```

覆盖 staging 后、最终 Guard 前完成 revoke/rotate/disable/delete/verification revoke/job cancel/task cancel、source/root/owner/batch/claim 变化；反向提交后的历史保留；BLOB 期间 deadline/lease/monotonic 耗尽；写入后 fencing/manifest 不变量损坏；outbox 插入失败及其后异常回滚；事务内与 COMMIT 后真实 fork/os._exit；replay、伪造可信绑定/commit context、failed package、绝对 lease、只读 registry、v3→v4 失败回滚与重复迁移。

进程退出测试观察实际 acquisition 表，不是 fake transaction；小型合成数据和离线 BytesIO receiver 不代替最大负载、真实 Unix transport 或生产权限验收。

## 边界与剩余工作

- **生产仍 NO-GO。** 未实现 Step 2.2-B/2.2-C；真实 Unix peer/socket、Gateway transport、完整 receipt lookup/reconciler/recovery、capability roots/no-follow 部署接线保留后续任务。
- Adapter 的 `receipt()` 当前明确拒绝并返回内部 `RECEIPT_PENDING` 异常，不能被当作已实现历史 receipt 查询；没有以 fake committed/not_committed 替代。
- 不自动清理 package/outbox/receipt，不迁移生产数据，不访问凭据，不联网、不部署、不修改 Agent、不 commit/push。
- 当前目录 Git 命令返回 `not a git repository`，无法独立核实用户给定基线 `e57d226ed54fa4bfec7e9e0d6a7b952e7576d8f5`。
- 尚未证明重启后的可信时钟恢复、完整路径权限、最大 RSS/锁等待/取消延迟/临时磁盘预算。不得把本次通过的逻辑测试当作 Step 2.5 生产验收。
