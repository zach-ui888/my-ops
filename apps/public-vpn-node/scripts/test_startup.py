"""Synthetic nft JSON and startup regressions; all OS/network actions mocked."""
import copy
from contextlib import ExitStack
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import MagicMock, patch

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE / 'app'))
import entrypoint
import firewall
import ingress
import ingress_guard
import nft_support
import vpn_runtime


def match(left, right, op='=='):
    return {'match': {'op': op, 'left': left, 'right': right}}


def guard_json():
    # Synthetic fixture for nft 1.0.x JSON: ct state is a bitmask membership.
    uid = match({'meta': {'key': 'skuid'}}, 65532)
    expressions = [
        [uid, match({'payload': {'protocol': 'ip', 'field': 'daddr'}}, '127.0.0.1'),
         match({'payload': {'protocol': 'tcp', 'field': 'dport'}}, {'set': [7928, 9443]}), {'accept': None}],
        [uid, match({'payload': {'protocol': 'tcp', 'field': 'sport'}}, 8443),
         match({'ct': {'key': 'direction'}}, 'reply'),
         match({'ct': {'key': 'state'}}, 'established', 'in'), {'accept': None}],
        [uid, {'counter': {'packets': 12, 'bytes': 800}}, {'drop': None}],
    ]
    entries = [{'metainfo': {'json_schema_version': 1}},
               {'table': {'family': 'inet', 'name': ingress_guard.TABLE, 'handle': 1}},
               {'chain': {'family': 'inet', 'table': ingress_guard.TABLE, 'name': 'output',
                          'handle': 2, 'type': 'filter', 'hook': 'output', 'prio': -10, 'policy': 'accept'}}]
    entries += [{'rule': {'family': 'inet', 'table': ingress_guard.TABLE, 'chain': 'output',
                         'handle': i + 3, 'expr': expr}} for i, expr in enumerate(expressions)]
    return {'nftables': entries}


class NftTests(unittest.TestCase):
    def verified(self, data):
        with patch.object(nft_support.subprocess, 'run', return_value=MagicMock(stdout=json.dumps(data))):
            return ingress_guard.verified()

    def test_target_json_state_membership(self):
        self.assertTrue(self.verified(guard_json()))

    def test_single_state_set_and_reordered_ports(self):
        data = guard_json()
        data['nftables'][4]['rule']['expr'][3]['match']['right'] = {'set': ['established']}
        data['nftables'][3]['rule']['expr'][2]['match']['right'] = {'set': [9443, 7928]}
        self.assertTrue(self.verified(data))

    def test_explicit_tcp_dependency(self):
        data = guard_json()
        data['nftables'][4]['rule']['expr'].insert(1, match({'meta': {'key': 'l4proto'}}, 'tcp'))
        self.assertTrue(self.verified(data))

    def test_every_boundary_mutation_rejected(self):
        mutations = [(3, 0, 0), (3, 1, '0.0.0.0'), (3, 2, {'set': [7928, 9443, 8787]}),
                     (4, 1, 443), (4, 2, 'original'), (4, 3, {'set': ['established', 'related']})]
        for row, expr, value in mutations:
            data = guard_json()
            data['nftables'][row]['rule']['expr'][expr]['match']['right'] = value
            with self.subTest(row=row, expr=expr):
                self.assertFalse(self.verified(data))

    def test_extra_missing_reordered_rules_rejected(self):
        for mode in ('extra', 'missing', 'order', 'drop'):
            data = guard_json()
            entries = data['nftables']
            if mode == 'extra':
                entries.append(copy.deepcopy(entries[3]))
            elif mode == 'missing':
                entries.pop()
            elif mode == 'order':
                entries[3], entries[5] = entries[5], entries[3]
            else:
                entries[5]['rule']['expr'][-1] = {'accept': None}
            self.assertFalse(self.verified(data))

    def test_wrong_scope_or_dormant_table_rejected(self):
        for row, key, value in [(1, 'flags', ['dormant']), (1, 'family', 'ip'),
                                (2, 'table', 'other'), (3, 'family', 'ip6')]:
            data = guard_json()
            item = next(iter(data['nftables'][row].values()))
            item[key] = value
            self.assertFalse(self.verified(data))

    def test_chain_mutations_rejected(self):
        for key, value in [('prio', 0), ('hook', 'input'), ('policy', 'drop'), ('type', 'nat')]:
            data = guard_json()
            data['nftables'][2]['chain'][key] = value
            self.assertFalse(self.verified(data))

    def test_malformed_expression_and_hidden_verdict_rejected(self):
        for expr in (None, [None], [{'counter': {}, 'accept': None}]):
            data = guard_json()
            data['nftables'][5]['rule']['expr'] = expr
            self.assertFalse(self.verified(data))

    def test_install_and_command_failure_distinct(self):
        cases = [(FileNotFoundError('TEST_ONLY'), nft_support.NftCommandError),
                 (PermissionError('TEST_ONLY'), nft_support.NftCommandError),
                 (subprocess.TimeoutExpired('nft', 5), nft_support.NftCommandError),
                 (subprocess.CalledProcessError(1, 'nft', stderr='TEST_ONLY'), nft_support.NftInstallError)]
        for module in (firewall, ingress_guard):
            for error, expected in cases:
                with patch.object(nft_support.subprocess, 'run', side_effect=error):
                    with self.assertRaises(expected):
                        module.install()

    def test_readback_command_failure(self):
        with patch.object(nft_support.subprocess, 'run', side_effect=subprocess.CalledProcessError(1, 'nft')):
            with self.assertRaises(nft_support.NftCommandError):
                ingress_guard.verified()

    def test_invalid_json_and_schema(self):
        for body in ('TEST_ONLY', '{}', 'null', '{"nftables": null}', '{"nftables": [null]}'):
            with patch.object(nft_support.subprocess, 'run', return_value=MagicMock(stdout=body)):
                with self.assertRaises(nft_support.NftJSONError):
                    ingress_guard.verified()

    def test_initial_kill_switch_readback(self):
        entries = []
        for name in ('output', 'postrouting'):
            entries += [{'chain': {'name': name, 'type': 'filter', 'hook': name, 'prio': 0, 'policy': 'accept'}},
                        {'rule': {'chain': name, 'expr': [match({'meta': {'key': 'mark'}}, firewall.MARK),
                                                        {'counter': {'packets': 1, 'bytes': 2}}, {'drop': None}]}}]
        with patch.object(nft_support, 'readback', return_value=entries):
            self.assertTrue(firewall.verified())
            self.assertFalse(firewall.verified('tun2'))
            entries[-1]['rule']['expr'][-1] = {'accept': None}
            self.assertFalse(firewall.verified())


class StartupTests(unittest.TestCase):
    def setup_stages(self, stack):
        stack.enter_context(patch.dict(os.environ, {'PUBLIC_INGRESS': '1'}))
        stack.enter_context(patch.object(entrypoint.os, 'umask'))
        stages = [('credentials.load', 'entrypoint.security.load_credentials'),
                  ('firewall.install', 'firewall.install'), ('firewall.verify', 'firewall.verified'),
                  ('ingress_guard.install', 'ingress_guard.install'), ('ingress_guard.verify', 'ingress_guard.verified'),
                  ('vpn_runtime.state_reset', 'vpn_runtime.STATE'),
                  ('vpn_runtime.start_monitor', 'vpn_runtime.start_monitor'), ('ingress.start', 'ingress.start')]
        calls = []
        mocks = {}
        for stage, target in stages:
            mock = stack.enter_context(patch(target))
            if stage == 'vpn_runtime.state_reset':
                mock = mock.unlink
            mock.side_effect = lambda *a, name=stage, **kw: calls.append(name) or True
            mocks[stage] = mock
        manager = stack.enter_context(patch('vpngate_manager.main'))
        output = stack.enter_context(patch('sys.stdout', new_callable=io.StringIO))
        return stages, mocks, calls, manager, output

    def test_success_order(self):
        with ExitStack() as stack:
            stages, _, calls, manager, _ = self.setup_stages(stack)
            self.assertEqual(entrypoint.main(), 0)
            self.assertEqual(calls, [s for s, _ in stages])
            manager.assert_called_once()

    def test_every_stage_failure_stops_and_redacts(self):
        names = ['credentials.load', 'firewall.install', 'firewall.verify', 'ingress_guard.install',
                 'ingress_guard.verify', 'vpn_runtime.state_reset', 'vpn_runtime.start_monitor', 'ingress.start']
        for name in names:
            with self.subTest(stage=name), ExitStack() as stack:
                stages, mocks, calls, manager, output = self.setup_stages(stack)
                mocks[name].side_effect = ValueError('TEST_ONLY_SECRET\nserver.json vless://TEST_ONLY')
                self.assertEqual(entrypoint.main(), 1)
                manager.assert_not_called()
                self.assertEqual(calls, names[:names.index(name)])
                log = output.getvalue()
                self.assertIn('stage=' + name, log)
                self.assertIn('error=ValueError', log)
                self.assertNotIn('TEST_ONLY', log)
                self.assertEqual(len(log.splitlines()), 1)

    def test_false_verification_refuses_start(self):
        for name in ('firewall.verify', 'ingress_guard.verify'):
            with ExitStack() as stack:
                _, mocks, _, manager, output = self.setup_stages(stack)
                mocks[name].side_effect = None
                mocks[name].return_value = False
                self.assertEqual(entrypoint.main(), 1)
                self.assertIn('reason=nft-readback-mismatch', output.getvalue())
                manager.assert_not_called()
                mocks['ingress.start'].assert_not_called()

    def test_safe_error_categories(self):
        for cls, code in [(nft_support.NftCommandError, 'nft-command-failed'),
                          (nft_support.NftInstallError, 'nft-ruleset-install-failed'),
                          (nft_support.NftJSONError, 'nft-json-invalid'),
                          (nft_support.NftMismatchError, 'nft-readback-mismatch')]:
            with patch('sys.stdout', new_callable=io.StringIO) as output:
                entrypoint.failure('ingress_guard.verify', cls('TEST_ONLY_SECRET'))
                self.assertIn(code, output.getvalue())
                self.assertNotIn('TEST_ONLY', output.getvalue())

    def test_listener_cleanup_on_bind_or_thread_failure(self):
        for fail in ('bind', 'thread'):
            with patch.dict(os.environ, REALITY_TARGET='example.com'), \
                    patch.object(ingress.socket, 'socket') as factory, \
                    patch.object(ingress.threading, 'Thread') as thread:
                listener = factory.return_value
                if fail == 'bind':
                    listener.bind.side_effect = OSError()
                else:
                    thread.return_value.start.side_effect = RuntimeError()
                with self.assertRaises((OSError, RuntimeError)):
                    ingress.start()
                listener.close.assert_called_once()


if __name__ == '__main__':
    unittest.main()
