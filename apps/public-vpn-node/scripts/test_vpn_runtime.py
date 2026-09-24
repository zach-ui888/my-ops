"""Offline runtime regressions: no real networking or runtime configuration."""
import json
from pathlib import Path
import socket
import sys
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE / 'app'))
import firewall
import vpn_runtime as runtime
import proxy_server
import healthcheck


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        (BASE / '.test-work').mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=BASE / '.test-work')
        self.addCleanup(self.temp.cleanup)
        self.state = patch.object(runtime, 'STATE', Path(self.temp.name) / 'state.json')
        self.state.start()
        self.addCleanup(self.state.stop)
        runtime.interface = runtime.owner = runtime.candidate = None
        runtime.sockets.clear()
        self.process = MagicMock(pid=123)
        self.process.poll.return_value = None

    def activate(self, dev):
        runtime.begin(self.process)
        runtime.event(self.process, f'TUN/TAP device {dev} opened')
        runtime.event(self.process, 'Initialization Sequence Completed')

    def kernel(self):
        from contextlib import ExitStack
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch.object(runtime.os, 'kill'))
        self.install = stack.enter_context(patch.object(firewall, 'install'))
        self.verify = stack.enter_context(patch.object(firewall, 'verified', return_value=True))
        self.identity = stack.enter_context(patch.object(runtime, 'tun_identity', return_value=12))
        self.routes = stack.enter_context(patch.object(runtime, 'routing_valid', return_value=True))
        self.setup = stack.enter_context(patch.object(runtime, 'setup_policy_routing'))

    def test_tun0(self):
        self.kernel()
        self.activate('tun0')
        self.assertEqual(runtime.require_interface(), 'tun0')
        self.setup.assert_called_once_with('tun0')
        self.assertTrue(runtime.healthy())

    def test_tun2(self):
        self.kernel()
        self.activate('tun2')
        sock = MagicMock()
        runtime.bind_socket(sock)
        sock.setsockopt.assert_any_call(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, b'tun2\0')
        self.install.assert_called_with('tun2')

    def test_reconnect_closes_old_sockets_and_updates_all_policy(self):
        self.kernel()
        self.activate('tun2')
        sock = MagicMock()
        runtime.bind_socket(sock)
        runtime.event(self.process, 'SIGUSR1[soft,ping-restart] received, process restarting')
        sock.close.assert_called_once()
        self.assertFalse(runtime.healthy())
        with self.assertRaises(OSError):
            runtime.require_interface()
        runtime.event(self.process, 'TUN/TAP device tun3 opened')
        runtime.event(self.process, 'Initialization Sequence Completed')
        self.setup.assert_called_with('tun3')
        self.install.assert_called_with('tun3')
        self.assertEqual(runtime.require_interface(), 'tun3')

    def test_no_interface(self):
        self.kernel()
        runtime.begin(self.process)
        with self.assertRaises(OSError):
            runtime.event(self.process, 'Initialization Sequence Completed')
        self.assertFalse(runtime.healthy())
        self.setup.assert_not_called()

    def test_missing_kernel_tun(self):
        self.kernel()
        self.identity.side_effect = OSError('missing')
        with self.assertRaises(OSError):
            self.activate('tun2')
        self.assertFalse(runtime.healthy())

    def test_policy_failure(self):
        self.kernel()
        self.setup.side_effect = OSError('policy failed')
        with self.assertRaises(OSError):
            self.activate('tun2')
        self.assertIsNone(runtime.interface)
        self.assertFalse(runtime.healthy())
        self.assertNotIn(('tun2',), [c.args for c in self.install.call_args_list])

    def test_nft_failure(self):
        self.kernel()
        self.install.side_effect = lambda dev=None: (_ for _ in ()).throw(OSError()) if dev else None
        with self.assertRaises(OSError):
            self.activate('tun2')
        self.assertIsNone(runtime.interface)
        self.assertFalse(runtime.healthy())

    def test_nft_readback_failure(self):
        self.kernel()
        self.verify.return_value = False
        with self.assertRaises(OSError):
            self.activate('tun2')
        self.assertIsNone(runtime.interface)
        self.assertFalse(runtime.healthy())

    def test_closed_gate_failure_revokes_sockets(self):
        self.kernel()
        self.activate('tun2')
        sock = MagicMock()
        runtime.bind_socket(sock)
        self.install.side_effect = OSError()
        with self.assertRaises(OSError):
            runtime.revoke()
        sock.close.assert_called_once()
        self.assertFalse(runtime.healthy())

    def test_unrelated_probe_cannot_publish(self):
        self.kernel()
        runtime.begin(self.process)
        runtime.event(MagicMock(), 'TUN/TAP device tun9 opened')
        self.assertIsNone(runtime.candidate)

    def test_dead_process_or_reused_interface_unhealthy(self):
        self.kernel()
        self.activate('tun2')
        self.process.poll.return_value = 1
        with self.assertRaises(OSError):
            runtime.require_interface()
        self.process.poll.return_value = None
        self.identity.return_value = 13
        self.assertFalse(runtime.healthy())

    def test_stale_heartbeat_unhealthy(self):
        self.kernel()
        self.activate('tun2')
        with patch.object(runtime.time, 'time', return_value=time.time() + 20):
            self.assertFalse(runtime.healthy())

    def test_health_does_not_trust_listeners(self):
        with patch.object(socket, 'create_connection') as connect:
            self.assertEqual(healthcheck.check(), 1)
        connect.assert_not_called()

    def test_health_detects_policy_or_firewall_loss(self):
        self.kernel()
        self.activate('tun2')
        with patch.object(runtime.os, 'kill'), patch.object(socket, 'create_connection'):
            self.assertEqual(healthcheck.check(), 0)
            self.routes.return_value = False
            self.assertEqual(healthcheck.check(), 1)
            self.routes.return_value = True
            self.verify.return_value = False
            self.assertEqual(healthcheck.check(), 1)

    def test_tcp_no_unready_fallback(self):
        sock = MagicMock()
        with patch.object(socket, 'socket', return_value=sock):
            with self.assertRaises(OSError):
                proxy_server.create_connection(('8.8.8.8', 443))
        sock.connect.assert_not_called()
        sock.close.assert_called_once()

    def test_dns_no_unready_system_fallback(self):
        sock = MagicMock()
        with patch.object(socket, 'socket', return_value=sock), patch.object(socket, 'getaddrinfo') as system:
            self.assertIsNone(proxy_server.resolve_dns_over_vpn('example.invalid'))
        sock.connect.assert_not_called()
        sock.send.assert_not_called()
        system.assert_not_called()

    def test_dynamic_rules_and_closed_rules(self):
        for dev in ('tun0', 'tun2', 'tun3'):
            rules = firewall.ruleset(dev)
            self.assertEqual(rules.count(f'oifname != "{dev}"'), 2)
            self.assertEqual(rules.count('counter drop'), 2)
        self.assertNotIn('oifname', firewall.ruleset())
        with self.assertRaises(ValueError):
            firewall.ruleset('eth0')

    def test_policy_commands_fail_at_every_stage(self):
        for failure in range(4):
            with patch.object(runtime, 'ip_json', side_effect=[[{'priority': 100, 'table': 100}], [{'table': 100}]]), patch.object(runtime, 'run') as run:
                run.side_effect = [MagicMock()] * failure + [OSError('failed')]
                with self.assertRaises(OSError):
                    runtime.setup_policy_routing('tun2')

    def test_policy_uses_mark_table_and_actual_interface(self):
        with patch.object(runtime, 'ip_json', return_value=[]), patch.object(runtime, 'run') as run, \
                patch.object(runtime, 'routing_valid', return_value=True):
            runtime.setup_policy_routing('tun2')
        run.assert_any_call('ip', '-4', 'route', 'replace', 'default', 'dev', 'tun2', 'table', '100')
        run.assert_any_call('ip', '-4', 'rule', 'add', 'priority', '100', 'fwmark', '0x56504e/0xffffffff', 'lookup', '100')

    def test_first_boot_absent_table_is_created_without_flush(self):
        with patch.object(runtime, 'ip_json', return_value=[]), patch.object(runtime, 'run') as run, \
                patch.object(runtime, 'routing_valid', return_value=True):
            runtime.setup_policy_routing('tun2')
        self.assertFalse(any('flush' in c.args for c in run.call_args_list))
        self.assertEqual(len(run.call_args_list), 2)

    def test_policy_readback_rejects_missing_or_wrong_route(self):
        rule = {'priority': 100, 'fwmark': '0x56504e', 'table': 100}
        for routes, expected in [([], False), ([{'dst': 'default', 'dev': 'eth0'}], False),
                                 ([{'dst': 'default', 'dev': 'tun2'}], True)]:
            with patch.object(runtime, 'ip_json', side_effect=[routes, [rule]]):
                self.assertEqual(runtime.routing_valid('tun2'), expected)

    def test_tun_kernel_identity_requires_up_tun_and_ipv4(self):
        link = {'ifindex': 12, 'flags': ['UP'], 'linkinfo': {'info_kind': 'tun', 'info_data': {'type': 'tun'}}}
        with patch.object(runtime, 'run', return_value=MagicMock(stdout=json.dumps([link]))), \
                patch.object(runtime, 'ip_json', return_value=[{'addr_info': [{'family': 'inet', 'local': '10.0.0.2'}]}]):
            self.assertEqual(runtime.tun_identity('tun2'), 12)
        with patch.object(runtime, 'run', return_value=MagicMock(stdout=json.dumps([link]))), patch.object(runtime, 'ip_json', return_value=[]):
            with self.assertRaises(OSError):
                runtime.tun_identity('tun2')
        for invalid in ({**link, 'flags': []}, {**link, 'linkinfo': {'info_kind': 'veth'}}):
            with patch.object(runtime, 'run', return_value=MagicMock(stdout=json.dumps([invalid]))):
                with self.assertRaises(OSError):
                    runtime.tun_identity('tun2')

    def test_nft_json_readback_matches_interface_and_both_hooks(self):
        entries = []
        for name in ('output', 'postrouting'):
            entries.append({'chain': {'name': name, 'type': 'filter', 'hook': name, 'prio': 0, 'policy': 'accept'}})
            entries.append({'rule': {'chain': name, 'expr': [
                {'match': {'op': '==', 'left': {'meta': {'key': 'mark'}}, 'right': firewall.MARK}},
                {'match': {'op': '!=', 'left': {'meta': {'key': 'oifname'}}, 'right': 'tun2'}},
                {'counter': {'packets': 0, 'bytes': 0}}, {'drop': None}]}})
        with patch.object(firewall.subprocess, 'run', return_value=MagicMock(stdout=json.dumps({'nftables': entries}))):
            self.assertTrue(firewall.verified('tun2'))
            self.assertFalse(firewall.verified('tun3'))
        with patch.object(firewall.subprocess, 'run', return_value=MagicMock(stdout=json.dumps({'nftables': entries[:2]}))):
            self.assertFalse(firewall.verified('tun2'))

    def test_manager_log_stream_activates_owned_interface(self):
        import vpngate_manager as manager
        self.kernel()
        self.process.stdout = iter(['TUN/TAP device tun2 opened\n', 'Initialization Sequence Completed\n'])
        # Hold EOF until the main consumer receives initialization.
        import threading
        release = threading.Event()
        def stream():
            yield 'TUN/TAP device tun2 opened\n'
            yield 'Initialization Sequence Completed\n'
            release.wait(2)
        self.process.stdout = stream()
        with patch.object(manager.subprocess, 'Popen', return_value=self.process), \
                patch.object(manager, 'openvpn_command', return_value=['openvpn']), \
                patch.object(manager, 'log_to_json'), patch.object(manager, 'update_handshake_status'):
            try:
                ok, _, proc = manager.run_openvpn_until_ready('synthetic', True, True, timeout=1)
                self.assertTrue(ok)
                self.assertIs(proc, self.process)
                self.setup.assert_called_once_with('tun2')
                self.assertEqual(runtime.require_interface(), 'tun2')
            finally:
                with runtime.LOCK:
                    runtime.owner = None
                release.set()

    def test_health_closed_listener_with_ready_vpn(self):
        with patch.object(runtime, 'healthy', return_value=True), \
                patch.object(socket, 'create_connection', side_effect=OSError):
            self.assertEqual(healthcheck.check(), 1)


if __name__ == '__main__':
    unittest.main(verbosity=1)
