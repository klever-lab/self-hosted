"""Keep the VPN endpoint and its egress policy aligned when profiles change."""

import base64
import json
from pathlib import Path
import socket
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import manage


class VPNEndpointTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.state = Path(temp.name)
        profile = self.state / 'wireguard.conf'
        profile.write_text('''[Interface]
PrivateKey = test-private-key
Address = 10.64.0.2/32
DNS = 1.1.1.1

[Peer]
PublicKey = test-public-key
Endpoint = new-vpn.example:443
''')
        profile.chmod(0o600)
        self.cfg = {'wireguard_profile': str(profile), 'wireguard_endpoint_ip': '8.8.8.8'}
        state_patch = patch.object(manage, 'STATE', self.state)
        state_patch.start()
        self.addCleanup(state_patch.stop)

    def assert_endpoint(self, secret, policy, address):
        config = json.loads(base64.b64decode(secret['data']['config.json']))
        self.assertEqual(config['endpoints'][0]['peers'][0]['address'], address)
        self.assertEqual(policy['spec']['egress'], [{
            'to': [{'ipBlock': {'cidr': address + '/32'}}],
            'ports': [{'protocol': 'UDP', 'port': 443}],
        }])
        saved = json.loads((self.state / 'config.json').read_text())
        self.assertEqual(saved['wireguard_endpoint'], 'new-vpn.example:443')
        self.assertEqual(saved['wireguard_endpoint_ip'], address)

    def test_changed_or_legacy_endpoint_refreshes_cached_address(self):
        for previous in (None, 'old-vpn.example:443'):
            with self.subTest(previous=previous):
                cfg = dict(self.cfg)
                if previous is not None:
                    cfg['wireguard_endpoint'] = previous
                answer = [(socket.AF_INET, socket.SOCK_DGRAM, 17, '', ('1.1.1.1', 443))]
                with patch.object(manage.socket, 'getaddrinfo', return_value=answer) as resolve:
                    secret, policy = manage.vpn(cfg)
                resolve.assert_called_once_with('new-vpn.example', 443, socket.AF_INET, socket.SOCK_DGRAM)
                self.assert_endpoint(secret, policy, '1.1.1.1')

    def test_unchanged_endpoint_keeps_cached_address(self):
        self.cfg['wireguard_endpoint'] = 'new-vpn.example:443'
        with patch.object(manage.socket, 'getaddrinfo', side_effect=AssertionError('Unexpected DNS query')):
            secret, policy = manage.vpn(self.cfg)
        self.assert_endpoint(secret, policy, '8.8.8.8')


if __name__ == '__main__':
    unittest.main()
