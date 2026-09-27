"""Container entrypoint. Never invoke on the host."""
import os
import sys
import security
import nft_support


def failure(stage, exc):
    # Fixed vocabulary only: str(exc), argv, stderr, paths and values are unsafe.
    reasons = {nft_support.NftCommandError: "nft-command-failed",
               nft_support.NftInstallError: "nft-ruleset-install-failed",
               nft_support.NftJSONError: "nft-json-invalid",
               nft_support.NftMismatchError: "nft-readback-mismatch",
               FileNotFoundError: "required-resource-missing",
               PermissionError: "permission-denied",
               ValueError: "invalid-configuration",
               KeyError: "required-setting-missing",
               OSError: "os-operation-failed",
               RuntimeError: "runtime-initialization-failed"}
    kind = type(exc) if type(exc) in reasons else Exception
    reason = reasons.get(kind, "initialization-failed")
    print(f"public-vpn-node: stage={stage} error={kind.__name__} reason={reason}; 拒绝启动。", flush=True)


def require_verified(value):
    if not value:
        raise nft_support.NftMismatchError()


def main():
    os.umask(0o077)
    stage = "credentials.load"
    try:
        security.load_credentials()
        stage = "firewall.install"
        import firewall
        firewall.install()
        stage = "firewall.verify"
        require_verified(firewall.verified())
        if os.environ.get("PUBLIC_INGRESS") == "1":
            stage = "ingress_guard.install"
            import ingress_guard
            ingress_guard.install()
            stage = "ingress_guard.verify"
            require_verified(ingress_guard.verified())
        stage = "vpn_runtime.state_reset"
        import vpn_runtime
        vpn_runtime.STATE.unlink(missing_ok=True)
        stage = "vpn_runtime.start_monitor"
        vpn_runtime.start_monitor()
        if os.environ.get("PUBLIC_INGRESS") == "1":
            stage = "ingress.start"
            import ingress
            ingress.start()
    except Exception as exc:
        failure(stage, exc)
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
