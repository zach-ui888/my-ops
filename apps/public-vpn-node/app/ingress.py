"""V1.1 fixed REALITY target relay; every outbound uses the V1 VPN gate."""
import os
import re
import socket
import threading

import proxy_server


def target_name(value):
    if not isinstance(value, str) or len(value) > 253 or not re.fullmatch(
            r'(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,63}', value):
        raise ValueError('Invalid target hostname')
    return value


def serve_client(client, target, slots):
    try:
        with client:
            client.settimeout(20)
            with proxy_server.create_connection((target, 443)) as upstream:
                proxy_server.relay(client, upstream)
    except Exception:
        pass  # No target, payload, or credential logging.
    finally:
        slots.release()


def start():
    # A public, fixed SNI setting, not an arbitrary destination selected by peers.
    target = target_name(os.environ['REALITY_TARGET'])
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(('127.0.0.1', 9443))
        listener.listen(32)
    except Exception:
        listener.close()
        raise
    slots = threading.BoundedSemaphore(32)

    def accept():
        while True:
            client, _ = listener.accept()
            if not slots.acquire(blocking=False):
                client.close()
                continue
            threading.Thread(target=serve_client, args=(client, target, slots), daemon=True).start()
    try:
        threading.Thread(target=accept, daemon=True).start()
    except Exception:
        listener.close()
        raise
