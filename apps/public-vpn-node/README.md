# public-vpn-node 中文部署与运维

当前已完成最终 release-prep，保留此前已完成人工验收的 VPN 安全架构。本次只做离线复验与发布材料整理，不执行 Docker、不影响现有 VPN 容器、不修改宿主网络。53 项回归测试通过，完整 GPL v3 正文、NOTICE、发布排除与全文件空白检查已完成；结果见 [测试报告](docs/测试报告.md)。After Snapshot 文件指纹见 `docs/After-Snapshot.sha256`，供人工 Approval 后首次发布到 `my-ops/apps/public-vpn-node`；尚未提交、推送或部署。

## 来源与上一阶段现场

固定上游 commit：`14b803685e67b7d50deff8ee768d042d96dcaab1`。
来源项目为 `woyaozuofeiji/vpngate-docker`，内部名称 AimiliVPN。
`reference/UPSTREAM_COMMIT` 记录该 SHA；`reference/source/` 是人工交付、只读的审查材料。文件指纹复核通过，不等同于重新验证 Git 对象。reference 不提交、不进入镜像、不作为运行目录，也不复制其 Git 元数据。

上一 Task 已留下主要安全代码、Dockerfile、Compose、许可证副本和审查报告；README/测试报告却仍写“源码未取得”。本次沿用代码修补并更新文档，详见 [续作记录](docs/续作记录.md)、[源码审查](docs/源码审查.md)、[测试报告](docs/测试报告.md)。

## 功能与安全边界

应用显式使用本机代理 → 容器代理 → 当前实际 TUN 接口 → VPNGate 公共节点 → 目标站点。它不是宿主全局 VPN，也不是供外部客户端接入的 VPN 服务端。

| 入口/组件 | 用途与限制 |
| --- | --- |
| `127.0.0.1:8787` | HTTP Web 管理，路径为管理员设置的 `/<secret_path>/`，需账号密码登录 |
| `127.0.0.1:7928` | 同一 TCP 端口识别 HTTP/CONNECT 和 SOCKS5；强制独立代理账号密码 |
| SOCKS5 | 只支持 CONNECT；使用 `socks5h` 由代理经隧道解析，未实现 UDP ASSOCIATE/BIND |
| IPv4 | 业务目标仅允许公共 IPv4；拒绝回环、内网、链路本地、组播及 IPv6 |
| Web | 获取/测试/选择节点、自动/国家/固定节点/收藏策略、出口检测、连接/断开；网页不能修改凭据或端口 |
| 节点筛选 | 每轮默认最多 60 行候选，最多 5 路握手测试；国家限制影响选用，后台仍测试全部候选；IP 归属过滤只支持“所有 IP” |
| 就绪检查 | 验证当前 TUN、OpenVPN 生命周期心跳、nftables、table 100、标记规则和监听端口；unhealthy 本身不会触发 Docker 重启 |

容器内 root 运行，`cap_drop: ALL` 后只加 `NET_ADMIN`（TUN、路由、nft/SO_MARK）、`NET_RAW`（兼容 SO_BINDTODEVICE）。不使用 privileged、host network 或 docker.sock。只暴露 `/dev/net/tun`，使用独立 bridge、只读根文件系统、受限 tmpfs、专用 data 卷、只读 secret、no-new-privileges。容器内监听 0.0.0.0 用于 Docker 映射，宿主发布仅回环；同网络容器及宿主其他用户仍可能访问入口，认证不可省略。

**Kill switch 保护代理 TCP 和代理 DNS，不是整容器默认拒绝。** 两类套接字发送前设置 SO_MARK 并绑定 当前实际 TUN 接口，nft output/postrouting 拒绝带该标记而出口不是 当前实际 TUN 接口 的包。安装规则失败先退出，不启动监听；设置标记/绑定失败关闭套接字；DNS 失败不走系统解析；策略路由安装失败终止连接。VPNGate HTTPS API 的系统 DNS、HTTPS 获取，以及公共节点 OpenVPN 传输/测速是明确的未标记控制面例外，用于无 VPN 时引导建连。宿主其他应用不受保护。尚不能静态证明内核断线、接口重建、已有 TCP 连接等全部行为，必须执行抓包验收。

节点配置先重建白名单，拒绝脚本、plugin、额外 config、管理监听和自定义路由等指令；缓存使用前再校验并重新生成地址/路径。仅接受固定 HTTPS API，验证证书、拒绝重定向及 HTTP 降级。公共节点不可信，可能观察目标与明文业务；使用端到端 HTTPS，不承诺匿名性。

## 目录

- `app/`：三个修改后的上游模块及 security、firewall、entrypoint，镜像运行目录 `/app`。
- `Dockerfile` / `compose.yaml`：独立安全部署；Compose 使用合法 YAML 的 JSON 子集。
- `scripts/`：凭据配置、活性检查、离线验证及管理员验收辅助工具。
- `docs/`：审查证据、测试报告、续作记录与人工验收。
- `licenses/`：完整 GPL v3 正文、原样上游声明及修改通知。
- `.env.example`：无真实秘密的说明；默认不需要 .env，密码不通过环境传入。
- `secrets/`、命名卷 `/data`：由管理员部署时创建，不提交、不上传；不要复用上游旧数据卷。

## 前置条件与凭据

以下部署动作仅供已有 Docker 权限的 root 管理员，在批准后于本项目目录操作。受限开发用户不执行 Docker、不加入 docker 组、不调整 socket、不提权。管理员应确认 Linux Docker/Compose、TUN 和容器 nftables 支持，端口 8787/7928 空闲；不为本项目重配宿主 SSH、防火墙、DNS、路由或其他服务。此前运维状态未由本次会话复验。

在管理员私密交互终端，关闭终端录制/调试追踪，用密码管理器生成五项值，然后执行：

```bash
cd /ops/apps/public-vpn-node
python3 scripts/configure_credentials.py
```

工具隐藏输入并独占创建 `secrets/credentials.json`，目录 0700、文件 0600；存在即拒绝覆盖，不读取旧秘密，不回显。**由 root 管理员创建并持有文件**，否则在剥离 DAC_OVERRIDE 的容器内 root 可能无法读取其他 UID 的 0600 文件，不能靠放宽模式解决。

| 字段 | 要求 |
| --- | --- |
| username、proxy_username | 各 4–64 位字母数字/下划线/连字符，建议不同 |
| password、proxy_password | 各至少 20 位随机可打印 ASCII，无空白；建议不同，不用可预测口令 |
| secret_path | 24–128 位随机字母数字；是额外路径保护，不能替代登录 |

最大长度 128；不接受空值、控制字符或 `REPLACE` 占位词。无内置管理/代理密码；缺失配置直接失败。VPNGate 的公开协议身份 vpn/vpn 与个人凭据无关。

真实秘密不放入聊天、Git、README、.env.example、命令行参数或测试输出。只读挂载仍是明文文件，Docker 管理员与容器 root 可访问。管理会话使用密码学随机 token、内存保存、8 小时过期、最多 128 个；重启失效。Cookie 为 HttpOnly/SameSite=Strict；回环 HTTP 无 Secure 属性，不直接开放公网。

## 人工构建与验收入口

先按 [完整人工 Docker 验收](docs/人工Docker验收.md) 逐项执行并记录结果。下列仅为起始命令，**本次未执行**：

```bash
cd /ops/apps/public-vpn-node
bash scripts/verify.sh
docker compose -p public-vpn-node config --quiet
docker compose -p public-vpn-node build
docker compose -p public-vpn-node up -d
docker compose -p public-vpn-node ps
```

构建会访问 Docker 基础镜像及 Debian APT 仓库，不下载上游应用代码；没有 pip 依赖、远程安装器或自动更新。`debian:12-slim` 和 APT 版本未固定，管理员应记录 RepoDigests、镜像 ID 和包版本；尚未做依赖漏洞评估。网络受限时构建失败应停下，不改系统或降低 TLS 校验。

## 日常访问与验证

本机浏览器访问 `http://127.0.0.1:8787/<secret_path>/`，手工填入密码管理器保存的路径并登录；根路径返回 404 属于预期。远程管理只用既有 SSH 服务的本地端口转发，例如在管理员客户端执行以下模板（替换用户名和服务器，不改服务器 SSH 配置）：

```bash
ssh -N -L 127.0.0.1:18787:127.0.0.1:8787 -L 127.0.0.1:17928:127.0.0.1:7928 ADMIN@SERVER
```

随后客户端管理地址为 `http://127.0.0.1:18787/<secret_path>/`；代理为 `127.0.0.1:17928`。辅助验收脚本固定使用服务器本机 7928，应在服务器私密终端执行。

```bash
python3 scripts/probe_proxy.py --protocol http
python3 scripts/probe_proxy.py --protocol socks5h
python3 scripts/check_runtime_logs.py
```

探测工具隐藏输入代理账号/密码，经 curl stdin 传递认证，不用 argv/环境存密码；默认只输出退出码和公开出口 IP。未授权请求必须失败，两种协议成功后还须对照宿主出口和容器抓包。普通 HTTP 及代理认证在本地 HTTP/SOCKS5 通道不加密，远程访问应放在 SSH 隧道内；HTTPS 业务使用 CONNECT。

网页日志接口返回空列表，容器仅输出固定生命周期消息，不输出路径、Cookie、密码或 OpenVPN 原始日志。日志工具只报告精确匹配及非预期日志行，不打印原文；最近 1000 行不能证明历史日志全部安全。网页登录错误与状态可供现场诊断，禁止导出带秘密的 HAR/配置/卷。

## 停止、恢复与凭据轮换

```bash
docker compose -p public-vpn-node stop
docker compose -p public-vpn-node start
docker compose -p public-vpn-node restart
```

`restart: unless-stopped` 用于退出后的恢复；手动 stop 后不会随 Docker 重启自动恢复，需 start。容器使用 init 回收子进程；主进程处理 SIGTERM 清理活动连接。节点连接偏好保存在专用卷，重启需重新握手，期间代理应阻断；会话需重新登录。

轮换凭据由管理员先 stop，在私密终端受控替换 secret（不在命令行写值、不输出旧文件），保持 root 所有和 0600/0700，然后 `docker compose -p public-vpn-node up -d --force-recreate`，重新测试认证与日志。配置工具只负责首次创建，避免自动覆盖。不可复用上游 ui_auth.json/旧卷作为迁移手段。

开机恢复只在已有 Docker 自动启动机制下验证，不为此安装 systemd 单元或重启 Docker。宿主重启验收应并入管理员已批准的维护窗口，避免影响 tg-codex-agent 等服务；在该窗口之前将此项标记“未验收”。

## 故障处理

- 入口退出：检查凭据文件存在、拥有者/模式、TUN/capability、nft 内核支持；禁止临时 privileged/host network 绕过。
- 没节点：检查控制面 API 可用性及 TLS；白名单、禁压缩、公共 IPv4 限制会降低节点兼容率，不自动放宽。
- 代理请求失败：检查 Web 的连接状态与路由、当前实际 TUN 接口；healthcheck 会校验内核安全条件；远端站点可用性仍需代理实测。
- DNS 失败：业务 DNS 固定经 当前实际 TUN 接口 到 8.8.8.8:53 UDP，只取 A 记录，拒绝截断/异常响应；无系统 DNS、TCP DNS 回退。节点封锁 DNS 时请求失败是预期关闭行为。
- 断线期间仍成功：先检查是否已切换至另一条正常 VPN；以抓包为准。若发现业务明文走 eth0 或目标域名在控制面 DNS 出现，停止服务并判验收失败。
- 代理/Web 线程故障：healthcheck 可报 unhealthy，但 Docker 不自动重启 unhealthy；管理员定位后重启本服务，不重启宿主 Docker。

## 本地测试、升级与卸载

```bash
bash scripts/verify.sh
```

验证只读取明确列出的公开源码/文档，不读取真实 .env、secret、会话、运行卷或密钥；只在项目 `.test-work/` 写 Python 编译缓存。敏感模式扫描不是泄密不存在的证明，人工 `/diff` 仍需复核。reference 指纹一致、忽略规则和 Dockerfile 白名单检查不等于已检查 Git 暂存区或实际 Docker 构建上下文；管理员提交前还须确认 reference 未纳入变更。

升级固定版本并审查差异、依赖及许可后重新验收。本项目未保存已验收版本；今后保留已验收镜像 ID、Compose 及兼容数据备份供回滚。卸载只操作本项目，不执行全局 prune，不删除卷：

```bash
docker compose -p public-vpn-node down
```

## 许可证与部署判定

项目保持 **GPL-3.0-or-later**：`licenses/GPL-3.0.txt` 为本地 `reference/GPL-3.0.txt` 的逐字节副本；`licenses/UPSTREAM-LICENSE.txt` 保留收到的省略版声明，`licenses/NOTICE.txt` 记录来源、修改日期、许可与担保声明。完整正文已加入 Docker 白名单，不需要发布 reference/。分发时保留这些材料并提供对应源码；镜像交付须另外落实 GPL 的源码提供要求。

本次 53 项离线测试通过，未复验真实运行环境；既有人工验收结论来自任务交接，不能把 mock 测试视作新一轮抓包证据。动态 TUN、fail-closed kill switch、fwmark policy routing、健康检查、OpenVPN 断线自动恢复和 HTTP/SOCKS5 逻辑保持不变。控制面直连例外、公共 VPN 信任边界、未固定依赖等限制继续适用。

当前目录无 Git 仓库，After Snapshot 使用公开文件白名单和 SHA-256 描述；全文件执行 `git diff --no-index --check`，没有关闭空白检测。发布时只使用清单文件和清单本身，不要整目录打包；secrets/、reference/、.test-work/、data/、runtime/、日志及真实 .env 均排除。实际 Git 暂存区仍由人工首次导入时核对，禁止使用强制添加绕过忽略规则。满足进入人工 Approval 的离线条件，不代表已发布或重新部署。
