"""Protect client identity, operator inputs, and the VPN's sole internet route."""

import base64
import configparser
from contextlib import redirect_stdout
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import friends_vpn as vpn


PRIVATE = base64.b64encode(bytes(range(32))).decode()
PUBLIC = base64.b64encode(bytes(reversed(range(32)))).decode()


class FriendsVPNTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        for target, name, value in ((vpn, 'DIRECTORY', self.directory),
                                    (vpn, 'STATE', self.directory / 'config.json'),
                                    (vpn.manage, 'STATE', self.directory)):
            setting = patch.object(target, name, value)
            setting.start()
            self.addCleanup(setting.stop)
        self.profile = self.directory / 'upstream.conf'
        self.profile.write_text(f'''[Interface]
PrivateKey = {PRIVATE}
Address = 10.64.0.2/32
DNS = 10.255.255.3
[Peer]
PublicKey = {PUBLIC}
Endpoint = vpn.example:443
AllowedIPs = 0.0.0.0/0
''')
        self.profile.chmod(0o600)

    def state(self):
        return {'windscribe_profile': str(self.profile), 'endpoint': '8.8.8.8:31820',
                'private_key': PRIVATE, 'next_peer': 2, 'peers': {}}

    def test_profile_must_be_private_and_outside_git(self):
        self.profile.chmod(0o644)
        with self.assertRaisesRegex(ValueError, '0600'):
            vpn.windscribe_profile(self.profile)
        self.profile.chmod(0o600)
        with patch.object(vpn.manage, 'ROOT', self.directory):
            with self.assertRaisesRegex(ValueError, 'outside Git'):
                vpn.windscribe_profile(self.profile)

    def test_copied_qbittorrent_key_is_refused(self):
        path = self.directory / 'config.json'
        path.write_text(json.dumps({'wireguard_profile': str(self.profile)}))
        path.chmod(0o600)
        with self.assertRaisesRegex(ValueError, 'qBittorrent already uses'):
            vpn.windscribe_profile(self.profile)

    def test_private_upstream_cannot_be_allowed_by_network_policy(self):
        provider = vpn.windscribe_profile(self.profile)
        state = {'provider_endpoint': 'vpn.example:443', 'provider_address': '100.100.1.2'}
        with self.assertRaisesRegex(ValueError, 'public IPv4'):
            vpn.provider_address(provider, state)

    def test_client_export_captures_ipv6_and_does_not_expose_keys(self):
        state = self.state()
        with patch.object(vpn, 'generate_key', return_value=PRIVATE), \
                patch.object(vpn, 'public_key', return_value=PUBLIC), \
                patch.object(vpn.manage, 'run') as run, redirect_stdout(io.StringIO()) as output:
            run.return_value.stdout = (PUBLIC + '\n').encode()
            vpn.add_peer(state, 'friend-phone')
        path = self.directory / 'friend-phone.conf'
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        profile = configparser.ConfigParser()
        profile.read(path)
        self.assertEqual(profile['Peer']['AllowedIPs'], '0.0.0.0/0, ::/0')
        self.assertEqual(profile['Interface']['DNS'], '1.1.1.1')
        self.assertNotIn(PRIVATE, output.getvalue())
        self.assertNotIn(PUBLIC, output.getvalue())
        self.assertEqual(state['peers']['friend-phone']['addresses'],
                         ['10.76.0.2/32', 'fd76:6672:6965::2/128'])
        before = path.read_bytes()
        with self.assertRaisesRegex(ValueError, 'already exists'):
            vpn.add_peer(state, 'friend-phone')
        self.assertEqual(path.read_bytes(), before)

    def test_client_name_cannot_write_outside_operator_directory(self):
        for name in ('../other', '/tmp/client', 'a/b', 'Client', ''):
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, 'lowercase'):
                vpn.add_peer(self.state(), name)

    def test_no_direct_route_or_privilege_is_needed(self):
        provider = vpn.windscribe_profile(self.profile)
        config = vpn.configuration(self.state(), provider, '8.8.8.8')
        self.assertNotIn('outbounds', config)
        self.assertEqual(config['route']['final'], 'windscribe')
        self.assertEqual(config['route']['rules'][0], {'inbound': ['windscribe'], 'action': 'reject'})
        self.assertIn({'ip_version': 6, 'action': 'reject'}, config['route']['rules'])
        self.assertIn({'ip_is_private': True, 'action': 'reject'}, config['route']['rules'])
        self.assertEqual(config['route']['rules'][-1], {'action': 'reject'})
        for item in config['endpoints']:
            self.assertFalse(item['system'])
        self.assertTrue(config['endpoints'][0]['udp_fragment'])
        policy = vpn.egress_policy(provider, '8.8.8.8')
        self.assertEqual(policy['spec']['egress'], [
            {'to': [{'ipBlock': {'cidr': '8.8.8.8/32'}}], 'ports': [{'protocol': 'UDP', 'port': 443}]}])

    def test_last_client_revocation_updates_inventory_without_restarting(self):
        state = self.state()
        state['inventory_migrated'] = True
        vpn.save(state)
        inventory = {'peers': {'friend': {'email': 'friend@example.com'}}, 'retired': {}}
        exported = self.directory / 'friend.conf'
        exported.write_text('private client profile')

        def update(mutation):
            self.assertTrue(mutation(inventory))
            self.assertFalse(mutation(inventory))
            return inventory

        with patch.object(sys, 'argv', ['friends-vpn', 'revoke', 'friend']), \
                patch.object(vpn.devices, 'update', side_effect=update), \
                patch.object(vpn.devices, 'wait_for_gateway', side_effect=lambda check: self.assertTrue(check(inventory))), \
                patch.object(vpn.manage, 'kube') as kube, redirect_stdout(io.StringIO()):
            vpn.main()
        self.assertFalse(exported.exists())
        self.assertTrue(exported.with_suffix('.conf.revoked').exists())
        self.assertEqual(inventory['peers'], {})
        self.assertEqual(inventory['retired'], {'friend': 'friend@example.com'})
        kube.assert_not_called()

    def test_seed_preserves_legacy_keys_and_never_overwrites_the_live_inventory(self):
        state = self.state()
        state['peers']['friend'] = {'public_key': PUBLIC, 'preshared_key': PRIVATE, 'addresses': vpn.devices.addresses(2)}
        state['next_peer'] = 3
        with patch.object(vpn, 'public_key', return_value=PUBLIC), \
                patch.object(vpn.devices, 'read', return_value=(None, None)), \
                patch.object(vpn.devices, 'seed') as seed:
            vpn.seed_inventory(state, None)
        seeded = seed.call_args.args[0]
        self.assertEqual(seeded['peers']['friend'], {**state['peers']['friend'], 'name': 'friend'})
        self.assertEqual(seeded['next_peer'], 3)
        self.assertTrue(vpn.read_state()['inventory_migrated'])
        with patch.object(vpn, 'public_key', return_value=PUBLIC), \
                patch.object(vpn.devices, 'read', return_value=(seeded, '1')), \
                patch.object(vpn.devices, 'seed') as seed:
            vpn.seed_inventory(state, None)
        seed.assert_not_called()

    def test_release_verifies_the_archive_before_staging_immutable_parts(self):
        data = b'fake archive'
        pin = {'tag': 'friends-vpn-123456789abc', 'size': len(data),
               'sha256': hashlib.sha256(data).hexdigest()}
        with patch.object(vpn.release, 'CHUNK_SIZE', 5), \
                patch.object(vpn.release, 'urlopen', return_value=io.BytesIO(data)), \
                patch.object(vpn.manage, 'apply') as apply:
            vpn.release.stage(pin)
        maps = apply.call_args.args[0]
        self.assertEqual(len(maps), 3)
        self.assertTrue(all(item['immutable'] for item in maps))
        self.assertEqual(b''.join(base64.b64decode(item['binaryData']['archive-part']) for item in maps), data)

        with patch.object(vpn.release, 'urlopen', return_value=io.BytesIO(b'x' * len(data))), \
                patch.object(vpn.manage, 'apply') as apply:
            with self.assertRaisesRegex(ValueError, 'checksum'):
                vpn.release.stage(pin)
        apply.assert_not_called()


if __name__ == '__main__':
    unittest.main()
