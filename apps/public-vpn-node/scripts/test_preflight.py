"""Offline security regression tests; synthetic data only, sockets are mocked."""
import ast
import copy
import io
import json
from pathlib import Path
import socket
import sys
import unittest
from unittest.mock import patch, MagicMock
from preflight import BASE, check, validate_compose
sys.path.insert(0, str(BASE / 'app'))
import security
import proxy_server
import healthcheck
import firewall
import vpngate_manager as manager
import subprocess
import base64
from probe_proxy import curl_auth

PROFILE = '''client
dev tun
proto tcp
remote 8.8.8.8 443
<ca>
-----BEGIN CERTIFICATE-----
VEVTVA==
-----END CERTIFICATE-----
</ca>
'''
# Synthetic values deliberately unrelated to any deployment credential.
FAKE = dict(username='test_user', password='TEST_ONLY_PASSWORD_00001',
            secret_path='TESTONLYPATH00000000000000',
            proxy_username='test_proxy', proxy_password='TEST_ONLY_PASSWORD_00002')


class PolicyTests(unittest.TestCase):
    def setUp(self):
        patcher = patch.object(proxy_server.vpn_runtime, "require_interface", return_value="tun2")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_project_preflight(self):
        self.assertEqual(check()[1], 0)

    def test_wrong_directory(self):
        self.assertEqual(check(BASE / 'absent')[1], 2)

    def test_reject_insecure_compose_mutations(self):
        base = json.loads((BASE / 'compose.yaml').read_text())
        for key, value in [('privileged', True), ('network_mode', 'host'),
                           ('cap_add', ['SYS_ADMIN']), ('ports', ['7928:7928']),
                           ('volumes', ['/var/run/docker.sock:/var/run/docker.sock']),
                           ('devices', []), ('read_only', False), ('security_opt', [])]:
            cfg = copy.deepcopy(base)
            cfg['services']['vpn'][key] = value
            with self.subTest(key=key):
                self.assertTrue(validate_compose(cfg))

    def test_profile_rebuild_idempotent(self):
        result = security.sanitize_config(PROFILE)
        self.assertEqual(security.sanitize_config(result), result)
        self.assertIn('script-security 0', result)
        self.assertIn('remote-cert-tls server', result)
        self.assertNotIn('persist-tun', result)

    def test_dangerous_profile_directives(self):
        for line in ['up /evil', 'plugin /evil', 'config /evil', 'management 0.0.0.0 9999',
                     'log /data/file', 'route 0.0.0.0 0.0.0.0', 'ca /secret',
                     'http-proxy 1.1.1.1 80', '<connection>', '--up /evil']:
            with self.subTest(line=line), self.assertRaises(ValueError):
                security.sanitize_config(PROFILE + line + '\n')

    def test_profile_blocks_invalid_remote_and_pem_injection(self):
        for value in ['127.0.0.1', '10.0.0.1', '169.254.169.254', '::1', 'example.com', '224.0.0.1']:
            with self.subTest(value=value), self.assertRaises(ValueError):
                security.sanitize_config(PROFILE.replace('8.8.8.8', value))
        with self.assertRaises(ValueError):
            security.sanitize_config(PROFILE.replace('VEVTVA==', '</ca>\nup /evil\n<ca>'))
        with self.assertRaises(ValueError):
            security.sanitize_config(PROFILE + 'remote 1.1.1.1 443\n')

    def test_credentials_required_and_placeholders_rejected(self):
        self.assertEqual(security.validate_credentials(FAKE), FAKE)
        for key in security.SECRET_FIELDS:
            for value in ['', 'REPLACE_ME', 'has\nnewline']:
                cfg = dict(FAKE, **{key: value})
                with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                    security.validate_credentials(cfg)

    def test_dns_failure_never_uses_system_resolver(self):
        with patch.object(proxy_server, 'resolve_dns_over_vpn', return_value=None), \
             patch.object(socket, 'getaddrinfo') as resolver:
            with self.assertRaises(OSError):
                proxy_server.create_connection(('example.invalid', 443))
            resolver.assert_not_called()

    def test_bind_failure_never_connects(self):
        sock = MagicMock()
        sock.setsockopt.side_effect = OSError(19, 'device unavailable')
        with patch.object(proxy_server, 'resolve_dns_over_vpn', return_value='8.8.8.8'), \
             patch.object(socket, 'getaddrinfo', return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('8.8.8.8', 443))]), \
             patch.object(socket, 'socket', return_value=sock):
            with self.assertRaises(OSError):
                proxy_server.create_connection(('example.invalid', 443))
        sock.connect.assert_not_called()
        sock.close.assert_called_once()

    def test_private_proxy_target_rejected(self):
        with patch.object(proxy_server, 'resolve_dns_over_vpn', return_value='127.0.0.1'), \
             patch.object(socket, 'socket') as factory:
            with self.assertRaises(ValueError):
                proxy_server.create_connection(('example.invalid', 80))
            factory.assert_not_called()

    def test_http_missing_auth_407_without_egress(self):
        client = MagicMock()
        client.recv.return_value = b'ET http://example.invalid/ HTTP/1.1\r\nHost: example.invalid\r\n\r\n'
        with patch.object(security, 'load_credentials', return_value=FAKE), \
             patch.object(proxy_server, 'create_connection') as connect:
            proxy_server.http_client(client, b'G')
        self.assertIn(b'407', client.sendall.call_args.args[0])
        connect.assert_not_called()

    def test_socks_noauth_rejected(self):
        client = MagicMock()
        client.recv.side_effect = [b'\x01', b'\x00']
        with patch.object(security, 'load_credentials', return_value=FAKE), \
             patch.object(proxy_server, 'create_connection') as connect:
            proxy_server.socks5_client(client, b'\x05')
        client.sendall.assert_called_with(b'\x05\xff')
        connect.assert_not_called()

    def test_healthcheck_closed_port_fails(self):
        with patch.object(socket, 'create_connection', side_effect=OSError):
            self.assertEqual(healthcheck.check(), 1)

    def test_firewall_failure_aborts_entrypoint(self):
        import entrypoint
        with patch.object(security, 'load_credentials', return_value=FAKE), \
             patch.object(firewall, 'install', side_effect=OSError), \
             patch.object(manager, "main") as start, \
             patch('sys.stdout', new_callable=io.StringIO) as output:
            self.assertEqual(entrypoint.main(), 1)
            for value in FAKE.values():
                self.assertNotIn(value, output.getvalue())
        start.assert_not_called()

    def test_proxy_socket_marked_before_connect(self):
        sock = MagicMock()
        with patch.object(proxy_server, 'resolve_dns_over_vpn', return_value='8.8.8.8'), \
             patch.object(socket, 'getaddrinfo', return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('8.8.8.8', 443))]), \
             patch.object(socket, 'socket', return_value=sock):
            self.assertIs(proxy_server.create_connection(('example.invalid', 443)), sock)
        calls = sock.method_calls
        mark_index = next(i for i, c in enumerate(calls) if c[0] == 'setsockopt' and c[1][1] == socket.SO_MARK)
        connect_index = next(i for i, c in enumerate(calls) if c[0] == 'connect')
        self.assertLess(mark_index, connect_index)

    def test_no_active_tls_downgrade_or_dns_repair(self):
        manager = (BASE / 'app/vpngate_manager.py').read_text()
        self.assertNotIn('_create_unverified_context', manager)
        self.assertNotIn('generate_random_password', manager)
        self.assertNotIn('ui_pass', manager)
        utils = ast.parse((BASE / 'app/vpn_utils.py').read_text())
        fn = next(n for n in utils.body if isinstance(n, ast.FunctionDef) and n.name == 'check_and_fix_dns')
        self.assertIsInstance(fn.body[0], ast.Return)



    def test_dns_mark_and_bind_fail_closed(self):
        for option in (socket.SO_MARK, socket.SO_BINDTODEVICE):
            sock = MagicMock()
            def fail(level, opt, value):
                if opt == option:
                    raise OSError('synthetic failure')
            sock.setsockopt.side_effect = fail
            with patch.object(socket, 'socket', return_value=sock), patch.object(socket, 'getaddrinfo') as resolver:
                self.assertIsNone(proxy_server.dns_query_over_vpn('example.invalid', 1, '8.8.8.8', 1))
            sock.connect.assert_not_called()
            sock.send.assert_not_called()
            resolver.assert_not_called()
            sock.close.assert_called_once()

    def test_dns_reply_validation_and_mark_order(self):
        question = b'\x07example\x07invalid\x00\x00\x01\x00\x01'
        response = b'\x12\x34\x81\x80\x00\x01\x00\x01\x00\x00\x00\x00' + question
        response += b'\xc0\x0c\x00\x01\x00\x01\x00\x00\x00\x01\x00\x04\x08\x08\x08\x08'
        for reply, expected in [(response, '8.8.8.8'), (b'xx' + response[2:], None),
                                (response[:2] + b'\x83' + response[3:], None),
                                (response.replace(b'example', b'changed'), None), (response[:14], None)]:
            sock = MagicMock()
            sock.recv.return_value = reply
            with patch.object(socket, 'socket', return_value=sock), patch.object(proxy_server.secrets, 'randbits', return_value=0x1234):
                self.assertEqual(proxy_server.dns_query_over_vpn('example.invalid', 1, '8.8.8.8', 1), expected)
            sock.connect.assert_called_once_with(('8.8.8.8', 53))
            methods = sock.method_calls
            mark = next(i for i, c in enumerate(methods) if c[0] == 'setsockopt' and c[1][1] == socket.SO_MARK)
            connect = next(i for i, c in enumerate(methods) if c[0] == 'connect')
            self.assertLess(mark, connect)

    def test_tcp_mark_failure_never_connects(self):
        sock = MagicMock()
        sock.setsockopt.side_effect = OSError('mark failed')
        with patch.object(socket, 'socket', return_value=sock):
            with self.assertRaises(OSError):
                proxy_server.create_connection(('8.8.8.8', 443))
        sock.connect.assert_not_called()
        sock.close.assert_called_once()

    def test_authenticated_http_strips_proxy_credentials(self):
        auth = base64.b64encode((FAKE['proxy_username'] + ':' + FAKE['proxy_password']).encode())
        client, upstream = MagicMock(), MagicMock()
        client.recv.return_value = b'ET http://example.invalid/a HTTP/1.1\r\nHost: example.invalid\r\nProxy-Authorization: Basic ' + auth + b'\r\n\r\n'
        with patch.object(security, 'load_credentials', return_value=FAKE), patch.object(proxy_server, 'create_connection', return_value=upstream) as connect, patch.object(proxy_server, 'relay'):
            proxy_server.http_client(client, b'G')
        connect.assert_called_once_with(('example.invalid', 80), timeout=20)
        forwarded = upstream.sendall.call_args.args[0]
        self.assertNotIn(auth, forwarded)
        self.assertNotIn(b'Proxy-Authorization', forwarded)
        self.assertTrue(forwarded.startswith(b'GET /a HTTP/1.1'))

    def test_unicode_proxy_auth_fails_without_exception(self):
        with patch.object(security, 'load_credentials', return_value=FAKE):
            self.assertFalse(proxy_server.check_credentials('非账号', '非密码'))

    def test_firewall_transaction_and_control_plane(self):
        with patch.object(firewall.subprocess, 'run') as run:
            firewall.install()
        kwargs = run.call_args.kwargs
        self.assertTrue(kwargs['check'])
        self.assertEqual(run.call_args.args[0], ['nft', '-f', '-'])
        self.assertNotIn('flush ruleset', kwargs['input'])
        self.assertIn('flush table inet public_vpn_node', kwargs['input'])
        self.assertIn('hook postrouting', kwargs['input'])
        # Unmarked discovery/transport is unaffected by both mark-specific drops.
        drops = [line for line in kwargs['input'].splitlines() if 'counter drop' in line]
        self.assertEqual(len(drops), 2)
        self.assertTrue(all('meta mark 0x56504e' in line and 'oifname' not in line for line in drops))

    def test_route_failure_is_not_reported_connected(self):
        with patch.object(manager.subprocess, 'run', side_effect=subprocess.CalledProcessError(1, 'ip')), patch.object(manager.time, 'sleep'):
            with self.assertRaises(RuntimeError):
                manager.setup_policy_routing("tun2")

    def test_cache_rebuilds_remote_and_path_and_rejects_injection(self):
        cache = [{'id': '../../test', 'config_text': PROFILE, 'config_file': '/forbidden', 'ip': '127.0.0.1'},
                 {'id': 'evil', 'config_text': PROFILE + 'up /evil'}]
        with patch.object(manager, 'read_json', return_value=cache):
            nodes = manager.read_nodes()
        self.assertEqual(len(nodes), 1)
        self.assertEqual(nodes[0]['ip'], '8.8.8.8')
        self.assertEqual(Path(nodes[0]['config_file']).parent, manager.CONFIG_DIR)
        self.assertNotIn('/', nodes[0]['id'])

    def test_web_cross_origin_denied_before_reading_secrets(self):
        handler = object.__new__(manager.Handler)
        handler.headers = {'Origin': 'http://attacker.invalid', 'Host': '127.0.0.1:8787'}
        handler.send_json = MagicMock()
        with patch.object(security, 'load_credentials') as credentials:
            handler.do_POST()
        self.assertEqual(handler.send_json.call_args.args[1], 403)
        credentials.assert_not_called()

    def test_web_missing_session_denied(self):
        handler = object.__new__(manager.Handler)
        handler.headers = {}
        with patch.object(manager, 'load_ui_config', return_value=FAKE):
            self.assertFalse(handler.is_authorized())

    def test_login_rate_limit_expires(self):
        with security._login_lock:
            security._login_times.clear()
        with patch.object(security.time, 'monotonic', return_value=100):
            self.assertTrue(all(security.allow_login() for _ in range(10)))
            self.assertFalse(security.allow_login())
        with patch.object(security.time, 'monotonic', return_value=161):
            self.assertTrue(security.allow_login())
        security._login_times.clear()

    def test_curl_auth_escaping_and_no_line_injection(self):
        self.assertEqual(curl_auth('test_user', 'a"b\\c'), 'proxy-user = "test_user:a\\"b\\\\c"\n')
        with self.assertRaises(ValueError):
            curl_auth('test_user', 'bad\nurl=evil')


if __name__ == '__main__':
    unittest.main(verbosity=2)
