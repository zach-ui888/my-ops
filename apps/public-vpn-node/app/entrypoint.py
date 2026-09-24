"""Container entrypoint. Never invoke on the host."""
import os
import sys
import security


def main():
    os.umask(0o077)
    try:
        security.load_credentials()
        import firewall
        firewall.install()
        import vpn_runtime
        vpn_runtime.STATE.unlink(missing_ok=True)
        vpn_runtime.start_monitor()
    except Exception:
        print('public-vpn-node: 凭据校验或容器防泄漏规则安装失败；拒绝启动。', flush=True)
        return 1
    # No inherited upstream proxies or command override are permitted.
    for key in tuple(os.environ):
        if key.lower().endswith('_proxy') or key.startswith('OPENVPN_UPSTREAM'):
            os.environ.pop(key, None)
    os.environ.update(OPENVPN_CMD='openvpn', VPNGATE_DATA_DIR='/data',
                      UI_HOST='0.0.0.0', UI_PORT='8787',
                      LOCAL_PROXY_HOST='0.0.0.0', LOCAL_PROXY_PORT='7928')
    print('public-vpn-node: 启动；仅输出生命周期事件，诊断请查看本机管理页面。', flush=True)
    try:
        import vpngate_manager
        vpngate_manager.main()
    except Exception:
        print('public-vpn-node: 服务异常退出；未输出异常内容。', flush=True)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
