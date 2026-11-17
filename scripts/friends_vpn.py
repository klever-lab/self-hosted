"""Manage a public WireGuard service with Windscribe as its only internet route."""

import argparse
import base64
import configparser
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import socket

import manage

NAMESPACE = 'friends-vpn'
DIRECTORY = manage.STATE / NAMESPACE
STATE = DIRECTORY / 'config.json'
# Capture both address families on clients; IPv6 is deliberately rejected until
# the provider supplies an IPv6 tunnel. Never let it escape through the local ISP.
SERVER_ADDRESSES = ['10.76.0.1/32', 'fd76:6672:6965::1/128']
BLOCKED = ['0.0.0.0/8', '100.64.0.0/10', '127.0.0.0/8', '169.254.0.0/16',
           '192.0.0.0/24', '198.18.0.0/15', '224.0.0.0/4', '240.0.0.0/4']


def read_private(path):
    path = Path(path).expanduser().resolve()
    if path.is_relative_to(manage.ROOT):
        raise ValueError('Keep VPN profiles and state outside Git')
    if path.stat().st_mode & 0o077:
        raise ValueError(f'{path} must have mode 0600')
    return path.read_text()


def read_state():
    return json.loads(read_private(STATE))


def save(state):
    manage.private_file(STATE, json.dumps(state, indent=2) + '\n')


def key(value):
    if len(base64.b64decode(value, validate=True)) != 32:
        raise ValueError('WireGuard keys must encode exactly 32 bytes')
    return value


def public_key(private):
    return manage.run(['wg', 'pubkey'], data=(key(private) + '\n').encode()).stdout.decode().strip()


def generate_key():
    return manage.run(['wg', 'genkey']).stdout.decode().strip()


def windscribe_profile(path):
    profile = configparser.ConfigParser()
    profile.read_string(read_private(path))
    if profile.sections() != ['Interface', 'Peer']:
        raise ValueError('Use a WireGuard profile with one Interface and one Peer')
    private = key(profile['Interface']['PrivateKey'].strip())
    # Refuse the existing downloader identity even when the profile was copied.
    app_config = manage.STATE / 'config.json'
    if app_config.exists():
        existing_path = json.loads(read_private(app_config)).get('wireguard_profile')
        if existing_path:
            existing = configparser.ConfigParser()
            existing.read_string(read_private(existing_path))
            if existing['Interface']['PrivateKey'].strip() == private:
                raise ValueError('Generate a new Windscribe key; qBittorrent already uses this one')
    addresses = [str(ipaddress.ip_interface(item.strip()))
                 for item in profile['Interface']['Address'].split(',')]
    ipv4 = [address for address in addresses if ipaddress.ip_interface(address).version == 4]
    if not ipv4:
        raise ValueError('The Windscribe profile needs an IPv4 tunnel address')
    host, port = profile['Peer']['Endpoint'].strip().rsplit(':', 1)
    if not 1 <= int(port) <= 65535:
        raise ValueError('Invalid Windscribe endpoint port')
    return {'private': private, 'addresses': ipv4, 'host': host, 'port': int(port),
            'public': key(profile['Peer']['PublicKey'].strip()),
            'preshared': key(profile['Peer']['PresharedKey'].strip()) if 'PresharedKey' in profile['Peer'] else ''}


def endpoint(value):
    host, port = value.rsplit(':', 1)
    if int(port) != 31820:
        raise ValueError('The public service uses UDP port 31820')
    # A public IPv4 endpoint avoids depending on an unrelated DNS service.
    if not ipaddress.IPv4Address(host).is_global:
        raise ValueError('Use the cloud controller\'s public IPv4 address')
    return value


def configuration(state, provider, address):
    peers = [{'public_key': peer['public_key'], 'pre_shared_key': peer['preshared_key'],
              'allowed_ips': peer['addresses']} for peer in state['peers'].values()]
    upstream = {'address': address, 'port': provider['port'], 'public_key': provider['public'],
                'allowed_ips': ['0.0.0.0/0'], 'persistent_keepalive_interval': 25}
    if provider['preshared']:
        upstream['pre_shared_key'] = provider['preshared']
    return {
        'log': {'level': 'warn', 'timestamp': True},
        'endpoints': [
            {'type': 'wireguard', 'tag': 'friends', 'system': False, 'mtu': 1280, 'udp_fragment': True,
             'address': SERVER_ADDRESSES, 'private_key': state['private_key'],
             'listen_port': 51820, 'peers': peers},
            {'type': 'wireguard', 'tag': 'windscribe', 'system': False, 'mtu': 1120,
             'address': provider['addresses'], 'private_key': provider['private'], 'peers': [upstream]},
        ],
        'route': {'rules': [
            {'inbound': ['windscribe'], 'action': 'reject'},
            {'ip_version': 6, 'action': 'reject'},
            {'ip_is_private': True, 'action': 'reject'},
            {'ip_cidr': BLOCKED, 'action': 'reject'},
            {'inbound': ['friends'], 'action': 'route', 'outbound': 'windscribe'},
            {'action': 'reject'},
        ], 'final': 'windscribe'},
    }


def provider_address(provider, state):
    endpoint_name = f'{provider["host"]}:{provider["port"]}'
    if state.get('provider_endpoint') == endpoint_name and state.get('provider_address'):
        address = state['provider_address']
    else:
        address = socket.getaddrinfo(provider['host'], provider['port'], socket.AF_INET, socket.SOCK_DGRAM)[0][4][0]
    if not ipaddress.IPv4Address(address).is_global:
        raise ValueError('Windscribe must resolve to a public IPv4 address')
    return address


def egress_policy(provider, address):
    return {'apiVersion': 'networking.k8s.io/v1', 'kind': 'NetworkPolicy',
            'metadata': {'name': 'windscribe-only', 'namespace': NAMESPACE},
            'spec': {'podSelector': {'matchLabels': {'app': NAMESPACE}}, 'policyTypes': ['Egress'],
                     'egress': [{'to': [{'ipBlock': {'cidr': address + '/32'}}],
                                 'ports': [{'protocol': 'UDP', 'port': provider['port']}]}]}}


def deploy(state):
    if not state['peers']:
        raise ValueError('Add at least one client before deploying')
    provider = windscribe_profile(state['windscribe_profile'])
    address = provider_address(provider, state)
    config = json.dumps(configuration(state, provider, address), sort_keys=True)
    items = manage.objects((NAMESPACE,))
    items.append(egress_policy(provider, address))
    manage.apply([obj for obj in items if obj['kind'] == 'Namespace'])
    manage.apply([obj for obj in items if obj['kind'] not in ('Namespace', 'Deployment')])
    manage.apply([manage.secret(NAMESPACE, NAMESPACE, {'config.json': config})])
    runtime = [obj for obj in items if obj['kind'] == 'Deployment']
    for obj in runtime:
        obj['spec']['template']['metadata']['annotations'] = {
            'self-hosted/config-sha256': hashlib.sha256(config.encode()).hexdigest()}
    manage.check_admission(runtime)
    manage.apply(runtime)
    state['provider_endpoint'] = f'{provider["host"]}:{provider["port"]}'
    state['provider_address'] = address
    save(state)
    manage.kube('-n', NAMESPACE, 'rollout', 'status', 'deployment/' + NAMESPACE, '--timeout=120s')
    print(f'Public WireGuard endpoint: {state["endpoint"]}')


def add_peer(state, name):
    if not re.fullmatch(r'[a-z][a-z0-9-]{0,31}', name):
        raise ValueError('Use a lowercase client name with letters, numbers, and hyphens')
    if name in state['peers']:
        raise ValueError('That client already exists; each device needs its own profile')
    number = state['next_peer']
    if not 2 <= number <= 254:
        raise ValueError('This VPN supports at most 253 allocated client addresses')
    private = generate_key()
    preshared = manage.run(['wg', 'genpsk']).stdout.decode().strip()
    addresses = [f'10.76.0.{number}/32', f'fd76:6672:6965::{number:x}/128']
    state['peers'][name] = {'public_key': public_key(private), 'preshared_key': preshared, 'addresses': addresses}
    state['next_peer'] += 1
    text = '\n'.join([
        '[Interface]', f'PrivateKey = {private}', 'Address = ' + ', '.join(addresses),
        'DNS = 1.1.1.1', 'MTU = 1280', '', '[Peer]',
        'PublicKey = ' + public_key(state['private_key']), 'PresharedKey = ' + preshared,
        'Endpoint = ' + state['endpoint'], 'AllowedIPs = 0.0.0.0/0, ::/0', 'PersistentKeepalive = 25', '',
    ])
    path = DIRECTORY / (name + '.conf')
    if path.exists():
        raise ValueError('An exported profile with that name already exists')
    manage.private_file(path, text)
    save(state)
    print(f'Private WireGuard client profile: {path}')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    init = commands.add_parser('init')
    init.add_argument('--windscribe-profile', required=True)
    init.add_argument('--endpoint', required=True)
    for name in ('add', 'revoke'):
        command = commands.add_parser(name)
        command.add_argument('name')
    for name in ('deploy', 'list', 'status', 'render'):
        commands.add_parser(name)
    args = parser.parse_args()
    os.umask(0o077)
    if DIRECTORY.resolve().is_relative_to(manage.ROOT):
        raise ValueError('Operator storage must be outside Git')
    DIRECTORY.mkdir(mode=0o700, parents=True, exist_ok=True)
    DIRECTORY.chmod(0o700)
    if args.command == 'render':
        import yaml
        print(yaml.safe_dump_all(manage.objects((NAMESPACE,)), sort_keys=False))
        return
    if args.command == 'init':
        if STATE.exists():
            raise ValueError('VPN configuration already exists; preserve the server and client keys')
        profile = str(Path(args.windscribe_profile).expanduser().resolve())
        windscribe_profile(profile)
        save({'windscribe_profile': profile, 'endpoint': endpoint(args.endpoint),
              'private_key': generate_key(), 'peers': {}, 'next_peer': 2})
        print(f'Protected VPN configuration: {STATE}')
        return
    state = read_state()
    if args.command == 'deploy':
        deploy(state)
    elif args.command == 'add':
        add_peer(state, args.name)
        print('Run ./friends-vpn deploy to activate this client.')
    elif args.command == 'revoke':
        if args.name not in state['peers']:
            raise ValueError('Unknown client')
        del state['peers'][args.name]
        save(state)
        path = DIRECTORY / (args.name + '.conf')
        if path.exists():
            path.rename(path.with_suffix('.conf.revoked'))
        # Revocation must remove the live peer, including when the last is removed.
        if state['peers']:
            deploy(state)
        else:
            manage.kube('-n', NAMESPACE, 'scale', 'deployment/' + NAMESPACE, '--replicas=0')
            manage.kube('-n', NAMESPACE, 'wait', '--for=delete', 'pod', '-l', 'app=' + NAMESPACE, '--timeout=90s')
        print(f'Revoked {args.name}.')
    elif args.command == 'list':
        print('Public endpoint: ' + state['endpoint'])
        for name, peer in state['peers'].items():
            print(name + ': ' + ', '.join(peer['addresses']))
    elif args.command == 'status':
        print(manage.kube('-n', NAMESPACE, 'get', 'deploy,pod,svc,networkpolicy', '-o', 'wide').stdout.decode())


if __name__ == '__main__':
    main()
