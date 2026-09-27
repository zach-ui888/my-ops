"""Offline V1.1 tests: synthetic credentials, no real sockets or system changes."""
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE / 'app'))
import ingress
import ingress_guard
import ingress_users as users
import vpn_runtime
from test_preflight import FAKE


def database():
    db = {'version': 1, 'host': '8.8.8.8', 'port': 443, 'sni': 'example.com',
          'private_key': 'A' * 43, 'public_key': 'B' * 43, 'users': {}}
    users.change(db, 'add', 'alice')
    users.change(db, 'add', 'bob')
    return db


class IngressTests(unittest.TestCase):
    def setUp(self):
        self.db = database()

    def test_unique_credentials(self):
        self.assertNotEqual(self.db['users']['alice']['id'], self.db['users']['bob']['id'])
        self.assertNotEqual(self.db['users']['alice']['short_id'], self.db['users']['bob']['short_id'])

    def test_duplicate_uuid_rejected(self):
        self.db['users']['bob']['id'] = self.db['users']['alice']['id']
        with self.assertRaises(ValueError):
            users.validate(self.db)

    def test_duplicate_short_id_rejected(self):
        self.db['users']['bob']['short_id'] = self.db['users']['alice']['short_id']
        with self.assertRaises(ValueError):
            users.validate(self.db)

    def test_user_revocation_preserves_other_identity(self):
        bob = copy.deepcopy(self.db['users']['bob'])
        users.change(self.db, 'disable', 'alice')
        cfg = users.server_config(self.db, FAKE)
        self.assertEqual([u['id'] for u in cfg['inbounds'][0]['settings']['clients']], [bob['id']])
        with self.assertRaises(ValueError):
            users.client_exports(self.db, 'alice')
        users.change(self.db, 'delete', 'alice')
        self.assertEqual(self.db['users']['bob'], bob)
        self.assertNotIn('alice', self.db['users'])

    def test_enable_restores_identity(self):
        before = self.db['users']['alice']['id']
        users.change(self.db, 'disable', 'alice')
        users.change(self.db, 'enable', 'alice')
        self.assertEqual(before, self.db['users']['alice']['id'])

    def test_readd_gets_new_identity(self):
        before = self.db['users']['alice']['id']
        users.change(self.db, 'delete', 'alice')
        users.change(self.db, 'add', 'alice')
        self.assertNotEqual(before, self.db['users']['alice']['id'])

    def test_no_enabled_users(self):
        for name in ('alice', 'bob'):
            users.change(self.db, 'disable', name)
        cfg = users.server_config(self.db, FAKE)
        self.assertEqual(cfg['inbounds'][0]['settings']['clients'], [])
        self.assertNotIn('', cfg['inbounds'][0]['streamSettings']['realitySettings']['shortIds'])

    def test_export_isolation(self):
        uri, clash = users.client_exports(self.db, 'alice')
        combined = uri + clash
        self.assertIn(self.db['users']['alice']['id'], combined)
        self.assertNotIn(self.db['users']['bob']['id'], combined)
        self.assertNotIn(self.db['private_key'], combined)
        self.assertNotIn(FAKE['proxy_password'], combined)
        self.assertIn('security=reality', uri)
        cfg = json.loads(clash)
        self.assertEqual(cfg['rules'], ['NETWORK,UDP,REJECT', 'MATCH,VPN'])
        self.assertFalse(cfg['proxies'][0]['udp'])
        self.assertNotIn('DIRECT', combined)
        self.assertTrue(cfg['dns']['nameserver'][0].endswith('#alice'))

    def test_only_socks_and_deny_outbound(self):
        cfg = users.server_config(self.db, FAKE)
        self.assertEqual([o['protocol'] for o in cfg['outbounds']], ['socks', 'blackhole'])
        self.assertEqual(cfg['outbounds'][0]['settings']['servers'][0]['address'], '127.0.0.1')
        self.assertEqual(cfg['routing']['domainStrategy'], 'AsIs')
        self.assertEqual(cfg['inbounds'][0]['streamSettings']['realitySettings']['target'], '127.0.0.1:9443')
        self.assertEqual(cfg['log']['loglevel'], 'none')

    def test_compatibility_keeps_reality_vless_vision_and_identity(self):
        before = copy.deepcopy(self.db)
        inbound = users.server_config(self.db, FAKE)['inbounds'][0]
        self.assertEqual(inbound['protocol'], 'vless')
        self.assertEqual(inbound['settings']['decryption'], 'none')
        self.assertTrue(all(c['flow'] == 'xtls-rprx-vision'
                            for c in inbound['settings']['clients']))
        self.assertEqual(inbound['streamSettings']['network'], 'tcp')
        self.assertEqual(inbound['streamSettings']['security'], 'reality')
        self.assertEqual(self.db, before)

    def test_target_validation(self):
        for value in ('localhost', '127.0.0.1', 'example.com:443', 'evil\n.com', '../escape'):
            with self.assertRaises(ValueError):
                ingress.target_name(value)

    def test_name_path_injection(self):
        for value in ('../escape', '/tmp/x', 'user\n', ''):
            with self.assertRaises(ValueError):
                users.change(self.db, 'add', value)

    def test_target_bridge_uses_guarded_connection(self):
        client, slots = MagicMock(), MagicMock()
        with patch.object(ingress.proxy_server, 'create_connection') as connect, \
                patch.object(ingress.proxy_server, 'relay') as relay:
            ingress.serve_client(client, 'example.com', slots)
            connect.assert_called_once_with(('example.com', 443))
            relay.assert_called_once()
        slots.release.assert_called_once()

    def test_target_bridge_failure_never_relays(self):
        with patch.object(ingress.proxy_server, 'create_connection', side_effect=OSError), \
                patch.object(ingress.proxy_server, 'relay') as relay:
            ingress.serve_client(MagicMock(), 'example.com', MagicMock())
            relay.assert_not_called()

    def test_uid_guard_closed_except_two_local_ports_and_inbound_replies(self):
        rules = ingress_guard.ruleset()
        self.assertIn('ip daddr 127.0.0.1 tcp dport { 7928, 9443 }', rules)
        self.assertIn('tcp sport 8443 ct direction reply ct state established', rules)
        self.assertIn('meta skuid 65532 counter drop', rules)
        self.assertNotIn('8787', rules)
        self.assertNotIn('udp', rules)
        self.assertNotIn('meta skuid 0 ', rules)

    def test_guard_missing_blocks_runtime(self):
        with patch.dict(os.environ, PUBLIC_INGRESS='1'), \
                patch.object(ingress_guard, 'verified', return_value=False):
            with self.assertRaises(OSError):
                vpn_runtime.require_interface()

    def test_guard_install_failure_propagates(self):
        with patch.object(ingress_guard.subprocess, 'run', side_effect=OSError):
            with self.assertRaises(OSError):
                ingress_guard.install()

    def test_guard_missing_rules_rejected(self):
        with patch.object(ingress_guard.subprocess, 'run', return_value=MagicMock(stdout='{"nftables": []}')):
            self.assertFalse(ingress_guard.verified())

    def test_atomic_private_write_and_symlink_rejection(self):
        (BASE / '.test-work').mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=BASE / '.test-work') as name:
            root = Path(name)
            dest = root / 'synthetic.json'
            users.write_file(dest, '{}')
            self.assertEqual(dest.stat().st_mode & 0o777, 0o600)
            self.assertEqual(dest.read_text(), '{}')
            (root / 'link').symlink_to(root, target_is_directory=True)
            with self.assertRaises(ValueError):
                users.private_dir(root / 'link')

    def test_key_generation_captures_output_without_private_argv(self):
        private = bytes.fromhex('302e020100300506032b656e04220420') + bytes(range(32))
        public = bytes.fromhex('302a300506032b656e032100') + bytes(range(32, 64))
        with patch.object(users.subprocess, 'run', side_effect=[MagicMock(stdout=private), MagicMock(stdout=public)]) as run:
            pair = users.keypair()
            self.assertEqual([len(k) for k in pair], [43, 43])
            self.assertEqual(run.call_args.kwargs['input'], private)
            self.assertNotIn(private, run.call_args.args[0])
            self.assertTrue(run.call_args.kwargs['capture_output'])
        with patch.object(users.subprocess, 'run', return_value=MagicMock(stdout=b'invalid')):
            with self.assertRaises(ValueError):
                users.keypair()

    def test_guard_readback_and_each_tampered_rule(self):
        def match(left, right):
            return {'match': {'op': '==', 'left': left, 'right': right}}
        uid = match({'meta': {'key': 'skuid'}}, 65532)
        expressions = [
            [uid, match({'payload': {'protocol': 'ip', 'field': 'daddr'}}, '127.0.0.1'),
             match({'payload': {'protocol': 'tcp', 'field': 'dport'}}, {'set': [7928, 9443]}), {'accept': None}],
            [uid, match({'payload': {'protocol': 'tcp', 'field': 'sport'}}, 8443),
             match({'ct': {'key': 'direction'}}, 'reply'),
             match({'ct': {'key': 'state'}}, 'established'), {'accept': None}],
            [uid, {'counter': {'packets': 0, 'bytes': 0}}, {'drop': None}],
        ]
        entries = [{'chain': {'name': 'output', 'type': 'filter', 'hook': 'output', 'prio': -10, 'policy': 'accept'}}]
        entries += [{'rule': {'chain': 'output', 'expr': expr}} for expr in expressions]
        with patch.object(ingress_guard.subprocess, 'run', return_value=MagicMock(stdout=json.dumps({'nftables': entries}))):
            self.assertTrue(ingress_guard.verified())
        for i in range(1, 4):
            changed = copy.deepcopy(entries)
            changed[i]['rule']['expr'][-1] = {'accept': None} if i == 3 else {'drop': None}
            with patch.object(ingress_guard.subprocess, 'run', return_value=MagicMock(stdout=json.dumps({'nftables': changed}))):
                self.assertFalse(ingress_guard.verified())

    def test_guard_failure_aborts_before_manager_start(self):
        import entrypoint
        with patch.dict(os.environ, PUBLIC_INGRESS='1'), \
                patch.object(entrypoint.security, 'load_credentials', return_value=FAKE), \
                patch('firewall.install'), patch('firewall.verified', return_value=True), \
                patch.object(ingress_guard, 'install', side_effect=OSError), \
                patch('vpngate_manager.main') as manager, patch('builtins.print'):
            self.assertEqual(entrypoint.main(), 1)
            manager.assert_not_called()

    def test_compose_boundaries(self):
        base = json.loads((BASE / 'compose.yaml').read_text())['services']['vpn']
        cfg = json.loads((BASE / 'compose.v1.1.yaml').read_text())['services']
        self.assertEqual(base['ports'], ['127.0.0.1:8787:8787', '127.0.0.1:7928:7928'])
        self.assertEqual(cfg['vpn']['ports'], ['0.0.0.0:443:8443/tcp'])
        x = cfg['ingress']
        self.assertEqual(x['network_mode'], 'service:vpn')
        self.assertEqual(x['cap_drop'], ['ALL'])
        self.assertNotIn('cap_add', x)
        self.assertFalse(x.get('privileged', False))
        self.assertEqual(x['security_opt'], ['no-new-privileges:true'])
        self.assertEqual(x['user'], '65532:65532')
        self.assertTrue(x['read_only'])
        self.assertEqual(x['restart'], 'unless-stopped')
        self.assertNotIn('ports', x)


if __name__ == '__main__':
    unittest.main()
