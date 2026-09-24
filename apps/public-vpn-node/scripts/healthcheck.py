"""Container readiness: live VPN, kernel safety policy, and listeners."""
import socket
import vpn_runtime


def check():
    try:
        if not vpn_runtime.healthy():
            return 1
        for port in (8787, 7928):
            with socket.create_connection(('127.0.0.1', port), timeout=1):
                pass
        return 0
    except OSError:
        return 1


if __name__ == '__main__':
    raise SystemExit(check())
