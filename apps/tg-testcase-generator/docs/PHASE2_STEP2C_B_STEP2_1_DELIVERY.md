# Phase 2 Step 2C-B Step 2.1 Delivery

本次按完整阅读后的 [DESIGN](PHASE2_STEP2C_B_STEP2_DESIGN.md) 和冻结的 [CONTRACT](PHASE2_STEP2C_B_STEP2_CONTRACT.md) 实现离线控制层。规范冲突以 CONTRACT 为准；没有修改冻结文档。

**结果：Step 2.1 离线实现已交付。生产接入仍为 NO-GO。** 新模块没有接入现有 receiver/acquisition 最终提交路径，不能把下面的 guard 单元测试当成现有 importer 已具备生产授权保护。

## 新增文件和责任

| 文件（均位于 `src/tg_testcase/`） | 已实现责任 |
|---|---|
| `contract_types.py` | 公共类型、严格 UTF-8 JSON、重复键/浮点数/非有限数/surrogate 拒绝、canonical bytes/SHA256 |
| `notion_reference.py` | §6 完整 ASCII grammar，UUID/ID/单段 URL 离线解析，标准化为 ID32 |
| `internal_errors.py` | §7 全部 35 个封闭错误、优先级、retryable、Application code 映射及白名单审计 DTO |
| `scope_policy.py` | V1 JSON schema、交叉引用、双向 database/data_source 登记、principal 根无歧义、集合规范化和 explicit-allow resolver |
| `credential_registry.py` | policy epoch CAS、规范字节存档、权威加载校验、credential head/history、verification 元数据、原子管理变更及 mutation 幂等；删除保留 tombstone 并清理 grant/principal 引用 |
| `registry_migrations.py` | 独立 registry schema v1；不复用 acquisition migration |
| `telegram_identity.py` | 九字段身份、受保护 peer 配置及注入接口、请求摘要、nonce/update 双唯一键、固定 Application request ID、持久化 Gateway ledger、时钟回退检测、reservation/result 引用及安全 GC |
| `job_contract.py` | 持久化 job 严格 DTO 和字段组不变量 |
| `job_registry.py` | 独立 SQLite FULL/foreign_keys、semantic/request 映射、活动 source 唯一性、状态机、claim intent/恢复、权威 attempt/fence、heartbeat/retry、不可变 pin、ACK/outbox 幂等消费及恢复规划 |
| `authorization.py` | 预建文件上的进程间 flock、固定 guard→registry→acquisition 锁顺序、权威 policy/generation 检查和仅 guard 持有期有效的 opaque commit decision |
| `controller_ports.py` | Acquisition/Notion typed ports、合成 SourceSnapshot、claim/outbox DTO、property 安全 locator DTO、lease 向下取整、单调时间 deadline budget；默认 lease 60s/heartbeat 20s |
| `controller_bridge.py` | 新安全入口到未改动 Application 的桥接；actor/owner/expected_version 映射、严格 reference 前置、当前授权后才重取缓存、owner 安全取消和 registry 取消传播 |
| `runtime_paths.py` | RuntimePaths/RuntimeOwners strict 配置；固定 DB/socket 名称、路径隔离、通过注入 verifier 检查父目录/symlink/ownership/权限/登录身份/runner 组；不访问生产路径 |

另新增：

- `tests/test_step21_contract.py`：合成契约测试；所有临时文件、registry、fake acquisition SQLite、Application Store 均在项目 `.test-runtime/` 下。
- 本交付文档。

原有 `src/`、`tests/`、`docs/`、`schemas/` 共 39 个文件逐项 SHA256 比对均未改变，包括 CONTRACT、DESIGN、public protocol、Application、acquisition、receiver、Store/storage、原 migration、package/content schemas 和图片/formal/completeness gate。

## 接口和恢复边界

管理员通过 `JobRegistry.publish()` 在同一 guard/事务内提交 credential/verification 元数据及完整 policy。首次 epoch=1，后续严格 +1；`mutate_credential()` 提供 rotate/enable/disable/delete 的原子代数更新。这里的 secret_handle 只是标签，没有 secret resolver。每次权威读取重新校验 policy digest 和当前 head/history；损坏拒绝继续。

可信组合层创建 `Peer` 和 peer 映射，传入 `IdentityLedger.reserve()`。JSON envelope 无法自报 peer UID、actor、request_id 或 credentials。实际 OS peer 认证留 Step 2.2。Gateway 同一 update 保留原 nonce/时间/action；过期未处理 update 不重新签发。Controller 先查持久化双键，再对新投递验证时限；已完成 retry 仍必须通过当前授权才能读取 Application 缓存。未完成 reservation 不 GC，Gateway ledger 当前保守保留、不自动删除。

`ControllerBridge(..., authorize_other=...)` 要求受保护调用方为 scope 之外的 Application 操作显式提供授权，尤其是正文/产物读取；scope policy 不隐式授予这些权限。bridge 保留 action 中的 expected_version，不用最新 task version 或 source revision 替换。Application 成功或失败回复完成 replay 记录；Application 已提交而 ledger 尚未完成时，以相同规范 request 重试。job 注册只接受已完成的成功 reservation，不把身份 reservation 当成 Application owner/version CAS 已成功的证明。

`JobRegistry.register()` 从可信 reservation、受保护 acquisition port 的 finalized source 和当前 policy 推导绑定。`start_claim()` 先持久化 intent，再调用 acquisition port，再保存其返回的 attempt/fence/lease，最后转 fetching。网络调用不在这里执行。崩溃恢复仅接受一致的 worker/revision/binding；旧 worker 不因新 attempt 恢复权限。pin 后可跨新 attempt 复用原 spool，但不能更换 package/digest/length/ref。

`authorize_commit(registry, job_id, claim, package_id, package_digest, now_ms)` 要求调用者已持 guard；它从 registry 读取 job，并检查当前 policy、credential、root 映射、cancel/deadline、claim/fence/lease 和 pin。返回的 opaque decision 不能经 JSON 构造，退出 guard 后失效。未来调用方必须一直持 guard 到 acquisition SQLite COMMIT，并在最终事务内重新检查 task owner/cancel、source revision/root、预算、deadline 和 lease。**现有 importer 尚未调用此接口。**

提交不确定时保留 reconciling；pending 不被当成 not_committed。恢复 planner 只通过注入 acquisition port 获取权威观察。已确认的历史 ACK/outbox/receipt 不要求当前 grant 或有效 sealed lease。真实的 metadata-only receipt lookup、outbox 同事务插入、缺 outbox 的受限完整对账及 receiver 只读 registry 接线均留 Step 2.2，不能把 fake port 当成其替代实现。

运行路径验证仅定义配置及注入 filesystem verifier 的接口；测试 fake metadata 不会把开发目录宣称为受保护部署。真正的 descriptor-relative/no-follow 路径打开、Store/storage roots 解耦及 receiver staging 注入属于 Step 2.2。property locator 算法按 CONTRACT §8 固定，Step 2.1 只提供错误及安全 DTO，算法/collision registry 不提前实现。

## 验证记录

环境：项目内 `.venv/bin/python`，Python 3.10.12；Pillow 11.3.0、pypdf 6.1.1、defusedxml 0.7.1。使用项目已有依赖，未联网安装。

命令：

```sh
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p 'test_step21_contract.py' -v
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -v
```

契约测试：**46 项通过**。覆盖以下关键行为：

- 完整 reference 接受/拒绝和长度/控制字符/编码边界；strict JSON、policy 类型、重复键、未知字段、跨 workspace 和多 root 歧义。
- 错 peer、伪造 actor/request ID、摘要与投递绑定、nonce/update 冲突、乱序 update、过期 retry、持久化时钟回退、并发 reservation 和较大合法 action 的 durable replay。
- epoch CAS、rotation/enable/disable/delete、mutation 幂等、tombstone、verification 撤销和失败事务的全量回滚。
- 真实 Application canonical ID32 写入、固定版本 CAS、跨 owner 拒绝、提交后 ledger 故障恢复、撤销后缓存授权和 owner 安全取消。
- semantic dedupe、禁止未接受动作建 job、claim 中断恢复、丢 lease/旧 fence、受 deadline 限制的 heartbeat、retry_wait、pin 跨 attempt 保持、outbox/ACK 幂等和冲突隔离。
- 未列状态边拒绝、提交不确定必须对账、spool 丢失先对账、历史提交不回退；两个 guard 实例及 fake acquisition SQLite COMMIT 与 revoke 的排序。
- fake filesystem 的 symlink/父目录写权限/UID/组/重叠路径/socket 长度和权限拒绝；lease 向下取整、单调 deadline；错误优先级及脱敏 DTO。

全量回归：**273 项通过，0 failures、0 errors、0 skipped**（27.418 秒）。契约测试为其中新增的 46 项，独立运行亦全部通过（3.543 秒）。测试原始日志位于项目 `.test-runtime/step21-contract-tests.log` 和 `.test-runtime/step21-full-tests.log`。

最初用系统 `python3` 执行旧测试时，因未加载项目依赖，出现两个 Pillow 导入错误和依赖相关跳过；切换既有项目 `.venv` 后已解决。没有用 stub、跳过配置或修改旧测试来绕过依赖。

## 后续工作与生产结论

- Step 2.2：真实 Unix peer transport、socket EOF/deadline/有限 writer、最终 acquisition commit/replay guard、实际 claim adapter、outbox migration、metadata receipt/reconciler、路径 roots 和 staging 接线。
- Step 2.3：每次 API 调用授权复核、typed fake Notion adapter、分页/预算/retry/scope proof、property locator/collision，以及 worker heartbeat/停止在途流。
- Step 2.4：隔离 downloader、SSRF、builder、规范 spool/pin/sender 和 spool 清理生命周期。
- Step 2.5：受保护部署模板、实际 UID/权限验证、最大合法输入 RSS/锁/取消/磁盘/崩溃恢复验收；本次没有编造验收阈值或进行生产迁移。

没有真实 Notion/Telegram 网络调用、凭据访问、生产部署、系统配置更改、Agent 修改或 git push。**生产接入仍为 NO-GO。**

冻结依据 SHA256（UTF-8 原始文件 bytes）：

- DESIGN：`eb6e6e953e49d7c7f4258b14109df91636f7b0355b1e3b04835e746cc66d42df`
- CONTRACT：`0e1a798bc2326a9dfd09b72e0572cd8891e09fc3383ee5f95666e8d29b150299`
