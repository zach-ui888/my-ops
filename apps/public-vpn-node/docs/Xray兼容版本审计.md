# V1.1 / Shadowrocket 2.2.92 的 Xray 固定审计

发布收尾更新（2026-09-27）：根据发布负责人提供的真实环境验收结果，VLESS + Reality 公网 TCP 443、多用户 add/disable/enable/delete、VPNGate Kill Switch、VPN 断线 fail-closed、自动重连换节点、Docker 服务重启恢复及端口边界均已通过。本次发布收尾仅执行离线验证，不重复运行验收；宿主整机重启、Mihomo 等未明确提供的结果不扩大认定。

Reality target/SNI 必须在部署时显式配置，并先做 TLS 1.3 兼容性验证；`REALITY_TARGET` 必须与 `secrets/ingress.json` 中 ingress state 的 `sni` 一致。`www.apple.com` 是本次经真实 Shadowrocket 验证可用的示例，不是默认值或永久兼容保证。此前 Microsoft target 在当前 Xray 26.3.27 + REALITY 实现中出现 TLS record length 超过 8192 的兼容问题；不能据此认定 Microsoft 普遍不兼容。

以下为 2026-09-24 历史源码审计与当时离线记录，其中“待验收”描述仅代表当时状态。新服务器恢复以 [恢复说明](V1.1部署与验收.md#8-新服务器恢复) 为准，V1.1 仅支持 TCP ingress，客户端 UDP forwarding 关闭。

审计日期：2026-09-24。正式版 **26.3.27**，平台 **linux/amd64**。
root 已完成镜像拉取及版本、平台、RepoDigest 核验（依据本次任务提供的结果）。
最终运行镜像：`ghcr.io/xtls/xray-core@sha256:592ec4d11f656db95598d01e76dbcc6e002d67360b96a5436500a938230f52c7`。
本次只修改公开文件和离线验证，不部署、不访问 secrets/runtime；
以下兼容源码分析沿用上一任务审计，本次未重新联网审计或进行真机握手验证。

## 1. 可复核的兼容边界

| 证据 | 结论 |
| --- | --- |
| [REALITY 8cdf7bf9c7f0](https://github.com/XTLS/REALITY/commit/8cdf7bf9c7f0)，2026-09-08 | 初始 key_share 必须先出现长度 1216 的 X25519MLKEM768；随后 X25519 可选。缺少前者，或先出现传统 X25519，均退出认证路径。supported_groups 宣告不能代替 key_share。 |
| [26.9.8 go.mod](https://github.com/XTLS/Xray-core/blob/v26.9.8/go.mod) | 引用 `v0.0.0-20260908062103-8cdf7bf9c7f0`，首次进入公开发布 tag 26.9.8。 |
| [26.7.28 go.mod](https://github.com/XTLS/Xray-core/blob/v26.7.28/go.mod) | 引用 `v0.0.0-20260322125925-9234c772ba8f`，仍接受传统 X25519；它是变更前最后一个发布 tag。 |
| [26.3.27 go.mod](https://github.com/XTLS/Xray-core/blob/v26.3.27/go.mod) 与 [REALITY tls.go](https://github.com/XTLS/REALITY/blob/9234c772ba8f181f31c3e81dc2b4177322e5a9a9/tls.go#L202-L216) | 同样引用 9234c772ba8f，先找长度 32 的 X25519；没有时才使用混合组中的 X25519。GREASE 不阻止找到传统组。 |
| [正式版 26.3.27](https://github.com/XTLS/Xray-core/releases/tag/v26.3.27) 与 [发布列表](https://github.com/XTLS/Xray-core/releases) | 审计时 26.3.27 为最近的非预发布版本；后续 26.4.25 至 26.9.9 标记为 Pre-release。 |

必须区分“正式版”与“有发布 tag”：26.9.8 首次携带强制限制，但上游将其标为
Pre-release；截至审计日，没有已核实携带此变更的非预发布正式版。
因此不能称“首次进入正式稳定版 26.9.8”。最后兼容发布 tag 是 26.7.28，
最后兼容的非预发布正式版是 **26.3.27**。按本任务要求只选择后者。
网页历史缓存可能停留在旧提交；结论以具体 release 的 go.mod 和固定修订源码为依据。

用户提供的 GREASE + X25519(0x001d, 32 bytes) 在 8cdf7bf 的检查中不能通过，
在 9234c77 的 key_share 提取逻辑中可以通过。这证明该失败条件得到移除，
不等于已证明 Shadowrocket 的认证、目标站握手及业务访问全部成功；仍需维护窗口实测。

## 2. 降级差异及风险

- Reality：恢复旧 ClientHello 的认证路径，同时失去新版对过时/异常指纹的拒绝策略。
  X25519-only TLS 密钥交换没有混合 ML-KEM 的后量子属性。传统认证仍存在，
  不关闭 Reality，不改变任何用户身份或服务器密钥，也不自动启用新的签名/加密选项。
- Reality 的两个额外回退必须接受：[修订比较](https://github.com/XTLS/REALITY/compare/9234c772ba8f...8cdf7bf9c7f0)
  显示丢失 [17 KiB target record buffer 修复](https://github.com/XTLS/REALITY/commit/393f8de3ee2d685271d79ee608334e441b2db324)
  和 [后台探测 panic/leak/race 修复](https://github.com/XTLS/REALITY/commit/e1986a4d31ca33c087a72ab4644aef1295693b69)。
  旧 buffer 为 8192；目标站较大握手记录可能失败，后台探测存在稳定性/资源风险。
  不能把这个降级称为仅改一个兼容开关。目标必须部署时显式配置并先做 TLS 1.3 兼容性验证，经原有本机 relay/VPN 访问；当前已验证示例及具体 Microsoft target 失败边界见页首。
- VLESS：保留现有 VLESS/TCP、用户 UUID、`decryption: none` 与 SOCKS5 出站；
  不为兼容而删除协议或另设直连出口。26.3.27 已具备 VLESS 后量子加密实现，
  但该可选协议功能不等于 Reality key_share，本项目不启用它。
- Vision：保留 `xtls-rprx-vision`。26.3.27 已包含
  [#5737 splice handoff 修复](https://github.com/XTLS/Xray-core/pull/5737)。
  [#5961](https://github.com/XTLS/Xray-core/issues/5961) 有针对该版的 padding panic 报告，
  属上游用户报告，本次未复现，也不声称 26.9.8 已解决。上线前需连接重试、长连接和负载验收，不能删除 Vision 绕过。
- 依赖从 26.9.8 的 Go 1.27 / x/crypto 0.55.0 / x/net 0.58.0 回退至
  26.3.27 的 Go 1.26（发布说明构建为 1.26.1）/ x/crypto 0.49.0 / x/net 0.52.0。
  不能假定后续安全修复仍保留；此审计不是完整漏洞审计或全部中间提交的等价性证明。
- UID 65532 Guard、Kill Switch、cap_drop ALL、无 privileged、只读挂载、443→8443、
  管理端口回环绑定均不改变。此处的兼容例外不允许扩大网络权限。

## 3. 已核验镜像锁定

`examples/xray.lock.json` 记录版本、linux/amd64、完整 RepoDigest 及
`verified-root-pull` 状态。`compose.v1.1.yaml` 的 image 使用上述纯 RepoDigest，
不带 tag。锁文件中的 tag 仅保留来源记录，不用于最终运行镜像选择。
`XRAY_IMAGE` 不参与镜像选择；禁止 latest、tag-only、tag@digest 或其它 digest。

```bash
python3 scripts/check_xray_pin.py
bash scripts/verify.sh
```

所有检查均要求精确匹配本次 root 核验值，不再接受 `--allow-pending`。
即使 Compose 与锁文件同时替换为同一个其它 digest，检查也必须失败。
离线检查不独立证明镜像上游归属；版本/平台取证来自 root 提供的核验结果。
Compose 静态解析需使用项目内空 Docker 配置目录和空 env 文件，禁用默认 .env 加载，
仅检查配置，不启动服务。Xray config test 只能使用该已拉取 digest、隔离网络和
合成测试配置，禁止挂载 secrets/runtime 或连接运行中容器；受限环境无法执行时由 root 后续完成。

## 4. 验收与回滚方案

本次允许的验收：Python 编译、公开 Compose JSON 结构、镜像锁定变异回归、原有 mock 测试、
公开白名单静态扫描与 whitespace 检查。未执行镜像内 Xray config test，未证明真机连通。
管理员另行授权维护窗口仅替换 ingress 镜像，保留现有所有秘密文件，
不 init/render、不更换 UUID/密钥/short ID、不改 vpn 镜像或宿主网络。
验证 Shadowrocket 2.2.92/iOS 17.4.1 握手、Vision 业务、长连接、VPN 出口和原有隔离验收。
本文件不提供或自动执行 up/down/restart。

本次未部署，无运行时回滚操作。后续若失败，恢复维护前保存的 ingress 镜像及公开配置，
已知旧镜像为 `ghcr.io/xtls/xray-core@sha256:3629bf7d825748cda29698ac354f8f2146f6b292edb9eb0c7cb7fe0583dae091`。
这是恢复原状态的故障回滚，不会修复旧客户端不兼容，且会被新兼容检查有意拒绝；
须作为明确记录的回滚例外处理，不能把它重新写成兼容基线。
不回退 UID Guard/Kill Switch，不删除数据卷，不停止 vpn，不修改任何秘密。
长期方案需客户端具备新握手能力后，重新审计正式版与 digest；禁止自动升级。

## 5. 本次精确 digest 固定回归结果

2026-09-24：`bash scripts/verify.sh` 全部通过，包含 compileall、preflight、
45 个公开文件静态检查、shell 语法、release checks 和严格 Xray pin 检查。
单元回归共 **100 项**：preflight 28、VPN runtime 25、ingress 23、startup 17、Xray pin 7。
Compose config --quiet 及合并 JSON 断言均通过：精确 image、UID 65532、cap_drop ALL、
只读根与挂载、service:vpn 网络及 7928/8787 回环、TCP 443 映射保持不变。
首次静态解析因缺少 REALITY_TARGET 拒绝，随后以 example.com 非秘密占位值通过；
全程使用空 env 文件、空 Docker 配置目录和隔离环境变量，未读取实际 .env 或 Docker 认证配置。

镜像只读查询被 Docker socket 权限拒绝，未执行 Xray config test，未尝试提权或绕过。
该项由 root 后续使用已拉取的精确 digest、--pull=never、--network=none、UID 65532、
cap_drop ALL、只读容器及项目内合成配置执行；不得挂载 secrets/runtime 或复用运行容器。
真实 Shadowrocket 握手及运行防泄漏仍待维护窗口验收。
本次未部署、未停止或重启服务、未修改网络规则及 secrets/runtime，未 Git commit/push。
