# 管理员人工 Docker 验收（尚未执行）

> 最终 release-prep 更新（2026-09-24）：下文保留历史审查/验收流程。完整 GPL 正文现已从本地 reference/GPL-3.0.txt 补齐到 licenses/GPL-3.0.txt，旧的“许可证正文缺失”阻塞已解除。既有架构已由人工验收（依据任务交接）；本次未运行 Docker 或重做运行验收。最新离线结果及发布边界以 [测试报告](测试报告.md) 为准。

本清单供已有 Docker 权限的 root 管理员在人工 `/diff`、`/approve` 后使用。不要由受限 Codex 执行。全部 Docker 操作限定 Compose 项目 `public-vpn-node`；不运行上游 install.sh，不下载上游源码，不改宿主 SSH/DNS/路由/防火墙或 tg-codex-agent。Docker 自身可能为 bridge/端口映射维护宿主网络规则，这是 Docker 正常行为，本项目不额外写宿主规则。

只使用合成测试流量；不录制秘密终端、不使用 `set -x`、不导出 HAR、真实配置或数据卷。抓包只看容器网络中测试包的摘要，不用 `-A/-X/-w`。以下命令按步骤逐条执行，不整页粘贴。记录 PASS/FAIL/未测、时间、镜像 ID 与不含秘密的证据；任一步失败则不接业务。

## 1. 静态结果、凭据、构建

```bash
cd /ops/apps/public-vpn-node
bash scripts/verify.sh
# 在私密交互终端首次创建；已有文件时工具拒绝覆盖。
python3 scripts/configure_credentials.py
stat -c '%a %u:%g %n' secrets secrets/credentials.json
docker compose -p public-vpn-node config --quiet
docker compose -p public-vpn-node build
docker image inspect public-vpn-node:14b8036-safe1 --format '{{.Id}} {{json .RepoDigests}}'
```

期待目录 700、文件 600、UID 0；不要 cat 凭据。config 不会展示文件内容，不运行无筛选的 inspect。保存构建使用的 Debian 基础镜像摘要/包版本；基础镜像和 APT 未锁定版本，构建不是完全可复现。许可证正文未补齐前不发布镜像。

## 2. 启动与最小权限

```bash
docker compose -p public-vpn-node up -d
docker compose -p public-vpn-node ps
vpn_cid=$(docker compose -p public-vpn-node ps -q vpn)
test -n "$vpn_cid"
docker inspect "$vpn_cid" --format 'Privileged={{.HostConfig.Privileged}} Network={{.HostConfig.NetworkMode}} ReadOnly={{.HostConfig.ReadonlyRootfs}}'
docker inspect "$vpn_cid" --format 'Drop={{json .HostConfig.CapDrop}} Add={{json .HostConfig.CapAdd}} Security={{json .HostConfig.SecurityOpt}}'
docker inspect "$vpn_cid" --format 'Devices={{json .HostConfig.Devices}} Ports={{json .HostConfig.PortBindings}} Restart={{.HostConfig.RestartPolicy.Name}}'
docker inspect "$vpn_cid" --format '{{range .Mounts}}{{.Type}} {{.Destination}} RW={{.RW}}{{println}}{{end}}'
docker compose -p public-vpn-node exec -T vpn sh -c 'id; grep -E "^(Cap(Inh|Prm|Eff|Bnd|Amb)|NoNewPrivs):" /proc/self/status; test -c /dev/net/tun; stat -c "%a %u:%g %n" /data /run/secrets/credentials; ss -lnt; ip rule; ip route show table 100; nft list table inet public_vpn_node'
docker compose -p public-vpn-node exec -T vpn dpkg-query -W python3 openvpn iproute2 nftables ca-certificates curl
```

期待 privileged=false、独立 bridge、只读根、cap drop ALL/add NET_ADMIN+NET_RAW，CapEff/CapBnd 为 `0000000000003000`，NoNewPrivs=1。不同容器运行时有差异则先分析，不直接补权限。只挂 TUN、专有 data 卷、只读 credentials，无 docker.sock/宿主系统目录。核验 secret 实际只读且容器可读；文件型 Compose secret 的模式以实际 bind mount 为准。nft output 和 postrouting 都存在标记丢弃规则。

监听只有 Web 8787 和代理 7928；OpenVPN 的临时传输套接字不是新增对外服务。宿主端口必须只发布 127.0.0.1，可补充 `docker compose ... port vpn 8787` / `port vpn 7928`。从另一台机器访问宿主公网 8787/7928 必须失败；不要为了通过测试新增宿主防火墙规则。

镜像运行文件排除检查：

```bash
docker compose -p public-vpn-node exec -T vpn sh -c 'test ! -e /app/reference; test ! -e /app/.git; test ! -e /app/.env; test ! -e /app/install.sh; test ! -e /usr/local/bin/ml'
```

## 3. Web、认证与会话

```bash
curl -q --noproxy '*' --silent --output /dev/null --write-out '%{http_code}\n' http://127.0.0.1:8787/
curl -q --noproxy '' --silent --output /dev/null --write-out '%{http_code}\n' --proxy http://127.0.0.1:7928 https://example.com/ --max-time 15
```

前者期待 404；后者未认证，期待代理 407/CONNECT 失败，不能成功访问目标。SOCKS5 无认证请求也必须失败：

```bash
curl -q --noproxy '' --silent --output /dev/null --proxy socks5h://127.0.0.1:7928 https://example.com/ --max-time 15
```

在私密浏览器手工输入 `/<secret_path>/`；未知路径 404、未登录 API 401，错误账号密码拒绝，10 次登录尝试/分钟后限速。等待一分钟后用正确凭据登录。确认节点列表、测试/选择/收藏/国家/固定模式、出口检测和断开操作可用。国家模式仍会测试其他候选但不得选择其他国家；“所有 IP”为唯一受支持归属选项。修改凭据入口只提示管理员操作，不能保存新密码或端口。

私密浏览器内查看 Cookie 属性 HttpOnly、SameSite=Strict、Max-Age=28800；不要复制 Cookie。退出登录后会话失效；重启后也应失效。跨 Origin POST 拒绝。无 HTTPS 的回环访问不声称 Secure Cookie。完整 XSS/CSRF/负载测试不在本次离线结果中，公网入口仍禁止。

## 4. HTTP、SOCKS5 与出口 IP

宿主直接出口只作对照，命令不上传项目文件：

```bash
curl -q --noproxy '*' --fail --silent --max-time 20 https://api.ipify.org
python3 scripts/probe_proxy.py --protocol http
python3 scripts/probe_proxy.py --protocol socks5h
```

工具隐藏输入，两种协议应成功并报告 VPN 出口，通常不同于宿主出口；相同或不同都不能独自证明无泄漏。通过 Web 查看活动节点，通过抓包确认目标业务只经 当前实际 TUN 接口。用错误凭据再测必须失败。

## 5. DNS 与出站抓包

需要宿主已具备 `nsenter`、`tcpdump`。缺失时停止该项并由管理员安排测试设施，不要求本项目安装宿主软件。nsenter 只进入目标容器的网络命名空间，不改网络设置。开两个私密终端：

```bash
vpn_cid=$(docker compose -p public-vpn-node ps -q vpn)
vpn_pid=$(docker inspect "$vpn_cid" --format '{{.State.Pid}}')
test "$vpn_pid" -gt 0
nsenter -t "$vpn_pid" -n tcpdump -ni eth0 -nn 'port 53 or tcp port 443'
```

```bash
vpn_cid=$(docker compose -p public-vpn-node ps -q vpn)
vpn_pid=$(docker inspect "$vpn_cid" --format '{{.State.Pid}}')
vpn_tun=$(docker compose -p public-vpn-node exec -T vpn python3 -c 'import vpn_runtime; import json; print(json.loads(vpn_runtime.STATE.read_text())["interface"])')
nsenter -t "$vpn_pid" -n tcpdump -ni "$vpn_tun" -nn 'port 53 or tcp port 443'
```

另一个终端分别通过 HTTP 与 socks5h 请求非敏感域名，并请求随机子域名（可不存在），避免缓存掩盖 DNS：

```bash
python3 scripts/probe_proxy.py --protocol http --url https://example.com/
python3 scripts/probe_proxy.py --protocol socks5h --url https://example.com/
python3 scripts/probe_proxy.py --protocol socks5h --url "https://probe-$(date +%s).example.com/" --expect blocked
```

期待业务 DNS 为 当前实际 TUN 接口→8.8.8.8:53，eth0 不能出现上述业务域名的明文查询。VPNGate API 自身系统 DNS 是允许例外，可能通过 Docker 内置解析器转发，不应把所有 53 流量一概当作业务泄漏。eth0 可见 VPN 节点加密传输和节点/API 控制面；按 Web 中的节点地址区分，必要时将 BPF 过滤扩大为该节点地址及验收目标地址。不要只看 IP 变化或“无 DNS 输出”便通过。

在临时关闭隧道后重复随机域名请求；DNS 失败须关闭请求，不允许退回宿主 DNS。仅 IPv4 A 查询，无 TCP DNS/AAAA 回退；有截断响应时失败属于设计行为。

## 6. 断线、已有连接、无隧道与内核后备阻断

先在 Web 选择“断开”以关闭自动连接，确认 当前实际 TUN 接口 不存在，然后做两类探测：

```bash
docker compose -p public-vpn-node exec -T vpn ip link show 当前实际 TUN 接口
python3 scripts/probe_proxy.py --protocol http --expect blocked --repeat 3
python3 scripts/probe_proxy.py --protocol socks5h --expect blocked --repeat 3
# 数字目标用于排除“只是 DNS 失败”的解释；健康隧道时先确认这个测试目标可用。
python3 scripts/probe_proxy.py --protocol http --url https://1.1.1.1/cdn-cgi/trace --expect blocked
python3 scripts/probe_proxy.py --protocol socks5h --url https://1.1.1.1/cdn-cgi/trace --expect blocked
```

`ip link` 在无 当前实际 TUN 接口 时非零为预期。所有请求不能普通直连。抓包确认；socket 在绑定阶段失败时 nft drop 计数可以不增加。

独立检查内核后备规则：仍保持断开，在容器内创建**带代理标记但故意不绑定 当前实际 TUN 接口**的合成 TCP 测试套接字；不能连通，nft 计数应增加。此步骤不删除或绕过防火墙规则：

```bash
docker compose -p public-vpn-node exec -T vpn python3 - <<'PYTEST'
import socket
from firewall import MARK
s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s.settimeout(3)
s.setsockopt(socket.SOL_SOCKET, socket.SO_MARK, MARK)
try:
    s.connect(('1.1.1.1', 443))
except OSError:
    print('marked TCP blocked; compare nft counters and capture')
else:
    raise SystemExit('FAIL: marked socket escaped tunnel')
finally:
    s.close()
PYTEST
docker compose -p public-vpn-node exec -T vpn nft list table inet public_vpn_node
```

没有默认路由时测试超时/失败不足以证明 nft；必须结合命中计数与 eth0 抓包。规则是 inet，也覆盖相同标记的 IPv6；应用业务 IPv6 本身被拒绝。

恢复 VPN 后，先启动非敏感、足够长的 HTTPS 下载，辅助脚本会限制到 1 KiB/s，只有状态与字节数输出。管理员替换以下公开测试 URL，避免私人 URL/token：

```bash
python3 scripts/probe_proxy.py --protocol http --stream --timeout 120 --url https://example.com/ADMIN_SELECTED_PUBLIC_LARGE_TEST_FILE
```

下载期间在另一个终端 Web 断开，观察**已有连接**结束/超时，eth0 不出现直连目标业务包；SOCKS5 同样重复。截断前已缓冲的数据可能继续送达，不能仅凭应用还有少量字节判断泄漏。请求原本未成功建立或文件瞬间结束，则此测试无效。

再恢复 VPN，测试突发接口消失（只在容器内）：

```bash
docker compose -p public-vpn-node exec -T vpn ip link delete 当前实际 TUN 接口
```

同时持续抓包及运行两个协议的新请求、已有长连接。自动重连/切换后可以恢复成功，但断线窗口不得 eth0 直连；确认新 当前实际 TUN 接口 是完成握手后的活动接口。再测试 OpenVPN 意外退出，只杀容器内 openvpn，不杀主程序或宿主进程：

```bash
docker compose -p public-vpn-node exec -T vpn python3 - <<'PYTEST'
from pathlib import Path
import os
import signal
count = 0
for p in Path('/proc').iterdir():
    if p.name.isdigit():
        try:
            if (p / 'comm').read_text().strip() == 'openvpn':
                os.kill(int(p.name), signal.SIGKILL)
                count += 1
        except (FileNotFoundError, ProcessLookupError):
            pass
print('terminated container OpenVPN processes:', count)
PYTEST
```

可能也会终止正在测速的 OpenVPN；这是隔离验收故障注入。核对自动/固定节点模式恢复、旧连接失败、新连接只在 VPN 恢复后成功。

## 7. 防火墙失败与凭据缺失启动

分别使用无网络、无权限的临时容器，不挂载真实秘密、不发布端口。第一条验证缺凭据直接退出；第二条以合成凭据加载结果测试 nft 安装失败是否阻止主程序。命令非零退出 1 为预期。

```bash
docker run --rm --network none --read-only --cap-drop ALL --security-opt no-new-privileges public-vpn-node:14b8036-safe1
docker run --rm --network none --read-only --cap-drop ALL --security-opt no-new-privileges --entrypoint python3 public-vpn-node:14b8036-safe1 -c 'import entrypoint; entrypoint.security.load_credentials=lambda: {}; raise SystemExit(entrypoint.main())'
```

第二项仅在临时容器中替代凭据读取以抵达真实 nft 失败路径，不代表凭据校验成功；正式服务不得这样启动。确认只有固定失败消息，没有继续监听、泄密或自动放宽规则。

## 8. 日志、重启与开机恢复

```bash
python3 scripts/check_runtime_logs.py
docker compose -p public-vpn-node restart
docker compose -p public-vpn-node ps
docker compose -p public-vpn-node exec -T vpn nft list table inet public_vpn_node
python3 scripts/probe_proxy.py --protocol http
python3 scripts/probe_proxy.py --protocol socks5h
python3 scripts/check_runtime_logs.py
```

重启后 nft 两条规则不应累加重复（入口事务只刷新自有表），无 VPN 时阻断，重新握手后可恢复；原会话失效、管理员凭据不变。先在 Web 恢复连接偏好，否则手动断开状态持久化后不会自动连接。

```bash
docker compose -p public-vpn-node stop
docker compose -p public-vpn-node start
docker inspect "$(docker compose -p public-vpn-node ps -q vpn)" --format '{{.HostConfig.RestartPolicy.Name}}'
```

验证正常 stop/start，仅本项目变化。`unless-stopped` 不会因 unhealthy 自动重启。开机恢复需在**另行批准的维护窗口**对宿主正常重启后重新执行 ps、权限/nft/端口、Web 登录及两类代理/DNS/出口验证；本清单不给出宿主 reboot 或 Docker 重启命令，以免影响其他服务。未安排维护窗口时，该项保持未验收，不声称验证完成。

## 9. 通过条件与回退

必须有：真实构建、仅回环监听、权限/挂载/TUN、强制认证、Web、两种代理出口、控制面可建连、DNS 抓包、主动断开/接口删除/OpenVPN 被杀后的新旧连接无泄漏、规则安装失败拒绝启动、日志无秘密、restart 结果。开机恢复单独记录，不可凭 restart 代替。

任何泄漏或权限异常：停止本项目并保留不含秘密的结果，不降低防护；不删 reference，不做全局清理。

```bash
docker compose -p public-vpn-node stop
```

当前这份清单的所有 Docker/抓包/浏览器步骤均为**未执行**，不能将离线 mock 测试算作人工验收。完整许可证正文仍待交付，阻止完成对外再分发。


## 动态接口修复追加验收

以下均由管理员在容器命名空间内验收，本次开发未执行：

- 分别观察实际分配 tun0、tun2 的连接；核对非敏感运行状态记录中的接口与 ifindex、`ip -d link`、IPv4 地址及 OpenVPN 的接口创建事件一致。不得把其他探测子进程的 TUN 当成业务接口。
- table 100 应仅有经当前 TUN 的默认路由；优先级 100 的规则以完整 fwmark 0x56504e/0xffffffff 查表 100。main 的默认路由仍经 eth0，供控制面使用，这是预期行为。
- output 与 postrouting 都必须拒绝业务标记从非当前 TUN 出站。未连接及切换阶段必须直接丢弃所有带业务标记的包。
- 触发 OpenVPN 自身重连并观察 tun2 → tun3：旧业务连接关闭；切换期 unhealthy；新接口、路由和 nft 回读通过后才恢复。重复 HTTP、SOCKS5 与随机域名 DNS 抓包，eth0 不得出现业务目标流量。
- 分别模拟接口丢失、策略路由缺失和 nft 安装失败；新代理连接必须拒绝，不能因监听端口存在而 healthy。只在隔离验收容器中注入故障，不修改宿主网络。
- 核对 Debian 12 镜像内 iproute2/nftables JSON 输出兼容性，检查首次无 table 100 时的 flush 行为。离线 mock 测试不能替代内核与抓包验证。
- 就绪检查不访问外网，不保证任意远端服务可达；通过原有代理验收工具确认真实出口连通性。
