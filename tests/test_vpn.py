"""Keep the VPN tunnel, firewall port forward, and pod security boundaries aligned."""

import base64
import configparser
import json
from pathlib import Path
import socket
import sys
import tempfile
import unittest
from unittest.mock import patch

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import manage

PRIVATE = base64.b64encode(bytes(range(32))).decode()
PUBLIC = base64.b64encode(bytes(reversed(range(32)))).decode()
PRESHARED = base64.b64encode(b'x' * 32).decode()

class VPNEndpointTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.state = Path(temp.name)
        profile = self.state / 'wireguard.conf'
        profile.write_text(f'''[Interface]
PrivateKey = {PRIVATE}
Address = 10.64.0.2/32, fd00::2/128
DNS = 10.255.255.3

[Peer]
PublicKey = {PUBLIC}
PresharedKey = {PRESHARED}
Endpoint = new-vpn.example:443
''')
        profile.chmod(0o600)
        self.cfg = {'wireguard_profile': str(profile), 'wireguard_endpoint_ip': '8.8.8.8'}
        state_patch = patch.object(manage, 'STATE', self.state)
        state_patch.start()
        self.addCleanup(state_patch.stop)

    def parse_wg(self, secret):
        parser = configparser.ConfigParser()
        parser.optionxform = str
        parser.read_string(base64.b64decode(secret['data']['wg.conf']).decode())
        return parser

    def assert_endpoint(self, secret, policy, address):
        wg = self.parse_wg(secret)
        self.assertEqual(wg['Interface']['PrivateKey'], PRIVATE)
        self.assertEqual(base64.b64decode(secret['data']['address']), b'10.64.0.2/32')
        self.assertNotIn('Address', wg['Interface'])
        self.assertEqual(wg['Peer']['PublicKey'], PUBLIC)
        self.assertEqual(wg['Peer']['PresharedKey'], PRESHARED)
        self.assertEqual(wg['Peer']['Endpoint'], f'{address}:443')
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

    def test_forward_opens_only_the_configured_vpn_peer_port_and_rotates_server_ip(self):
        self.cfg.update(wireguard_endpoint='new-vpn.example:443', qbittorrent_forwarded_port=10837)
        answers = [
            (socket.AF_INET, socket.SOCK_DGRAM, 17, '', ('8.8.8.8', 443)),
            (socket.AF_INET, socket.SOCK_DGRAM, 17, '', ('1.1.1.1', 443)),
        ]
        with patch.object(manage.socket, 'getaddrinfo', return_value=answers) as resolve:
            secret, policy = manage.vpn(self.cfg)
        resolve.assert_called_once_with('new-vpn.example', 443, socket.AF_INET, socket.SOCK_DGRAM)
        self.assertEqual(base64.b64decode(secret['data']['peer-port']), b'10837')
        self.assertEqual(base64.b64decode(secret['data']['forwarded-port']), b'10837')
        self.assert_endpoint(secret, policy, '1.1.1.1')
        saved = json.loads((self.state / 'config.json').read_text())
        self.assertEqual(saved['wireguard_forwarded_port'], 10837)
        with patch.object(manage.socket, 'getaddrinfo', side_effect=AssertionError('Unexpected DNS query')):
            manage.vpn(saved)

    def test_no_forward_leaves_vpn_input_ports_closed(self):
        self.cfg['wireguard_endpoint'] = 'new-vpn.example:443'
        secret, _ = manage.vpn(self.cfg)
        self.assertEqual(base64.b64decode(secret['data']['peer-port']), b'6881')
        self.assertEqual(base64.b64decode(secret['data']['forwarded-port']), b'')

    def test_invalid_or_conflicting_peer_port_cannot_change_configuration(self):
        for port in (True, '10837', -1, 0, 53, 8080, 65536):
            with self.subTest(port=port), self.assertRaisesRegex(ValueError, 'forwarded_port'):
                manage.vpn(dict(self.cfg, qbittorrent_forwarded_port=port))
        self.assertFalse((self.state / 'config.json').exists())

    def test_bad_keys_are_rejected_without_printing_them_or_updating_state(self):
        profile = Path(self.cfg['wireguard_profile'])
        profile.write_text(profile.read_text().replace(PRIVATE, 'invalid-private-material'))
        with self.assertRaisesRegex(ValueError, '32-byte keys') as error:
            manage.vpn(self.cfg)
        self.assertNotIn('invalid-private-material', str(error.exception))
        self.assertFalse((self.state / 'config.json').exists())

    def test_qbittorrent_remains_unprivileged_and_bound_to_wg0(self):
        docs = [doc for doc in yaml.safe_load_all((ROOT / 'apps/qbittorrent/stack.yaml').read_text()) if doc]
        deployment = next(doc for doc in docs if doc['kind'] == 'Deployment' and doc['metadata']['name'] == 'qbittorrent')
        spec = deployment['spec']['template']['spec']
        for host_key in ('hostNetwork', 'hostPID', 'hostIPC'):
            self.assertFalse(spec.get(host_key))
        for volume in spec['volumes']:
            self.assertNotIn('hostPath', volume)
        pod_sec = spec['securityContext']
        self.assertTrue(pod_sec['runAsNonRoot'])
        self.assertEqual(pod_sec['runAsUser'], 1000)
        containers = {c['name']: c for c in spec['initContainers'] + spec['containers']}
        for name in ('configure', 'webui', 'qbittorrent'):
            sec = containers[name]['securityContext']
            self.assertFalse(sec['allowPrivilegeEscalation'])
            self.assertTrue(sec['readOnlyRootFilesystem'])
            self.assertEqual(sec['capabilities'], {'drop': ['ALL']})
            self.assertNotIn('privileged', sec)
            self.assertNotIn('runAsUser', sec)
        vpn_sec = containers['vpn']['securityContext']
        self.assertFalse(vpn_sec['allowPrivilegeEscalation'])
        self.assertNotIn('privileged', vpn_sec)
        self.assertEqual(vpn_sec['capabilities'], {'drop': ['ALL'], 'add': ['NET_ADMIN']})
        self.assertTrue(vpn_sec['readOnlyRootFilesystem'])
        self.assertNotIn('restartPolicy', containers['vpn'])
        self.assertNotIn('env', containers['vpn'])
        self.assertEqual([c['name'] for c in spec['containers']], ['qbittorrent'])
        port_volume = next(v for v in spec['volumes'] if v['name'] == 'peer-port')
        self.assertEqual(port_volume['secret']['items'], [{'key': 'peer-port', 'path': 'peer-port'}])
        bootstrap = (ROOT / 'apps/qbittorrent/bootstrap.py').read_text()
        self.assertIn("'Session\\\\Interface': 'wg0'", bootstrap)
        self.assertIn("'Session\\\\InterfaceName': 'wg0'", bootstrap)
        self.assertIn("'Proxy\\\\Type': 'None'", bootstrap)


if __name__ == '__main__':
    unittest.main()
