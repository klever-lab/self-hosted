"""Manage a public WireGuard service with Windscribe as its only internet route."""

import argparse
import base64
import configparser
from datetime import datetime, timezone
import fcntl
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import tempfile
from uuid import uuid4

import friends_vpn_cloudflare as cloudflare
import friends_vpn_inventory as devices
import friends_vpn_release as release
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


def write_private(path, content):
    with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, delete=False) as output:
        temporary = Path(output.name)
        try:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


def save(state):
    write_private(STATE, json.dumps(state, indent=2) + '\n')


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


def api_egress_policy():
    service = json.loads(manage.kube('-n', 'default', 'get', 'svc', 'kubernetes', '-o', 'json').stdout)
    slices = json.loads(manage.kube('-n', 'default', 'get', 'endpointslice',
                                   '-l', 'kubernetes.io/service-name=kubernetes', '-o', 'json').stdout)

    def target(address, port):
        network = str(ipaddress.ip_network(address))
        return {'to': [{'ipBlock': {'cidr': network}}], 'ports': [{'protocol': 'TCP', 'port': port}]}

    targets = [target(service['spec']['clusterIP'], next(port['port'] for port in service['spec']['ports']
                                                       if port['name'] == 'https'))]
    # Account for Service DNAT before or after policy evaluation.
    for item in slices['items']:
        for port in item['ports']:
            if port['name'] == 'https':
                targets.extend(target(address, port['port']) for endpoint in item['endpoints']
                               for address in endpoint['addresses'])
    return {'apiVersion': 'networking.k8s.io/v1', 'kind': 'NetworkPolicy',
            'metadata': {'name': 'kubernetes-inventory', 'namespace': NAMESPACE},
            'spec': {'podSelector': {'matchExpressions': [{'key': 'app', 'operator': 'In',
                                                           'values': [NAMESPACE, NAMESPACE + '-portal']}]},
                     'policyTypes': ['Egress'], 'egress': targets}}


def seed_inventory(state, restore):
    server_public = public_key(state['private_key'])
    current, _ = devices.read(optional=True)
    if current is not None and restore:
        raise ValueError('The shared inventory already exists')
    if current is None:
        if restore:
            current = json.loads(read_private(restore))
        elif state.get('inventory_migrated'):
            raise ValueError('The shared inventory is missing. Restore a protected inventory backup.')
        else:
            current = {'peers': {name: {**peer, 'name': name} for name, peer in state['peers'].items()},
                       'next_peer': state['next_peer'], 'retired': {}, 'blocked_emails': {},
                       'server_public_key': server_public, 'endpoint': state['endpoint']}
        if current['server_public_key'] != server_public:
            raise ValueError('The inventory belongs to a different WireGuard server key')
        current['revision'] = uuid4().hex
        current['endpoint'] = state['endpoint']
        devices.seed(current)
    if current['server_public_key'] != server_public:
        raise ValueError('The inventory belongs to a different WireGuard server key')
    if current['endpoint'] != state['endpoint']:
        def set_endpoint(inventory):
            inventory['endpoint'] = state['endpoint']
            return True

        devices.update(set_endpoint)
    state['inventory_migrated'] = True
    save(state)


def deploy(state, restore=None):
    provider = windscribe_profile(state['windscribe_profile'])
    address = provider_address(provider, state)
    config = json.dumps(configuration({**state, 'peers': {}}, provider, address), sort_keys=True)
    items = manage.objects((NAMESPACE,))
    artifact = release.read()
    release.mount(items, artifact)
    items.append(egress_policy(provider, address))
    items.append(api_egress_policy())
    manage.apply([obj for obj in items if obj['kind'] == 'Namespace'])
    manage.apply([obj for obj in items if obj['kind'] not in ('Namespace', 'Deployment')])
    runtime = [obj for obj in items if obj['kind'] == 'Deployment']
    for obj in runtime:
        obj['spec']['template']['metadata']['annotations'] = {
            'self-hosted/config-sha256': hashlib.sha256(config.encode()).hexdigest()}
    manage.check_admission(runtime)
    release.stage(artifact)
    seed_inventory(state, restore)
    manage.apply([manage.secret(NAMESPACE, NAMESPACE, {'config.json': config})])
    manage.apply(runtime)
    state['provider_endpoint'] = f'{provider["host"]}:{provider["port"]}'
    state['provider_address'] = address
    save(state)
    manage.kube('-n', NAMESPACE, 'rollout', 'status', 'deployment/' + NAMESPACE, '--timeout=120s')
    print(f'Public WireGuard endpoint: {state["endpoint"]}')


def profile_text(inventory, peer, private):
    return '\n'.join([
        '[Interface]', f'PrivateKey = {private}', 'Address = ' + ', '.join(peer['addresses']),
        'DNS = 1.1.1.1', 'MTU = 1280', '', '[Peer]',
        'PublicKey = ' + inventory['server_public_key'], 'PresharedKey = ' + peer['preshared_key'],
        'Endpoint = ' + inventory['endpoint'], 'AllowedIPs = 0.0.0.0/0, ::/0', 'PersistentKeepalive = 25', '',
    ])


def new_key_pair():
    return {'private': generate_key(), 'preshared_key': manage.run(['wg', 'genpsk']).stdout.decode().strip()}


def client_name(name):
    if not re.fullmatch(r'[a-z][a-z0-9-]{0,31}', name):
        raise ValueError('Use a lowercase client name with letters, numbers, and hyphens')


def add_peer(state, name, email=''):
    client_name(name)
    path = DIRECTORY / (name + '.conf')
    if name in state['peers'] or path.exists() or path.with_suffix('.conf.revoked').exists():
        raise ValueError('That client already exists; each device needs its own profile')
    keys = new_key_pair()
    peer = {'public_key': public_key(keys['private']), 'preshared_key': keys['preshared_key'],
            'addresses': devices.addresses(state['next_peer']), 'email': email}
    state['peers'][name] = peer
    state['next_peer'] += 1
    write_private(path, profile_text({**state, 'server_public_key': public_key(state['private_key'])}, peer, keys['private']))
    save(state)
    print(f'Private WireGuard client profile: {path}')


def add_live_peer(name, email):
    client_name(name)
    path = DIRECTORY / (name + '.conf')
    pending = DIRECTORY / ('.' + name + '.pending.json')
    if pending.exists():
        keys = json.loads(read_private(pending))
        if keys['email'] != email:
            raise ValueError('Retry this pending profile with its original email')
    else:
        inventory, _ = devices.read()
        if name in inventory['peers'] or name in inventory['retired'] or path.exists():
            raise ValueError('That device ID already exists. Choose a new name.')
        if inventory['blocked_emails'].get(email):
            raise ValueError('Invite this email before assigning it a new device')
        devices.addresses(inventory['next_peer'])
        keys = {**new_key_pair(), 'email': email}
        write_private(pending, json.dumps(keys))
    public = public_key(keys['private'])

    def enroll(inventory):
        if inventory['blocked_emails'].get(email):
            raise ValueError('Invite this email before assigning it a new device')
        existing = inventory['peers'].get(name)
        if existing and existing['public_key'] == public and existing.get('email', '') == email:
            return False
        if existing or name in inventory['retired']:
            raise ValueError('That device ID already exists. Choose a new name.')
        inventory['peers'][name] = {'name': name, 'email': email, 'public_key': public,
                                    'preshared_key': keys['preshared_key'],
                                    'addresses': devices.addresses(inventory['next_peer'])}
        inventory['next_peer'] += 1
        return True

    inventory = devices.update(enroll)
    devices.wait_for_gateway(lambda current: current['peers'].get(name, {}).get('public_key') == public)
    write_private(path, profile_text(inventory, inventory['peers'][name], keys['private']))
    pending.unlink()
    print(f'Active WireGuard client profile: {path}')


def archive_profile(identity):
    if re.fullmatch(r'[a-z][a-z0-9-]{0,31}', identity):
        path = DIRECTORY / (identity + '.conf')
        if path.exists():
            path.rename(path.with_suffix('.conf.revoked'))
        (DIRECTORY / ('.' + identity + '.pending.json')).unlink(missing_ok=True)


def revoke_live(name=None, *, email='', block=False):
    def matches(identity, peer):
        return identity == name if name else peer.get('email') == email

    if name and not re.fullmatch(r'[a-z0-9-]{1,36}', name):
        raise ValueError('Use the device ID shown by friends-vpn device list')

    def remove(inventory):
        if name and name not in inventory['peers'] and name not in inventory['retired']:
            raise ValueError('Unknown device ID')
        selected = [identity for identity, peer in inventory['peers'].items() if matches(identity, peer)]
        changed = bool(selected)
        if block and not inventory['blocked_emails'].get(email):
            inventory['blocked_emails'][email] = True
            changed = True
        for identity in selected:
            devices.retire(inventory, identity)
        return changed

    inventory = devices.update(remove)
    devices.wait_for_gateway(lambda current: (not block or current['blocked_emails'].get(email)) and
                             not any(matches(identity, peer) for identity, peer in current['peers'].items()))
    for identity, owner in inventory['retired'].items():
        if matches(identity, {'email': owner}):
            archive_profile(identity)


def assign(name, email):
    def assign_email(inventory):
        if name not in inventory['peers']:
            raise ValueError('Unknown device ID')
        if inventory['blocked_emails'].get(email):
            raise ValueError('Invite this email before assigning it a device')
        peer = inventory['peers'][name]
        if peer.get('email') == email:
            return False
        peer['email'] = email
        return True

    devices.update(assign_email)
    print(f'Assigned {name} to {email}.')


def cloudflare_api():
    token = DIRECTORY / 'cloudflare-api-token'
    if not token.exists():
        raise ValueError(f'Save a scoped Cloudflare API token with mode 0600 at {token}')
    return cloudflare.Cloudflare(read_private(token))


def cloudflare_settings():
    path = DIRECTORY / 'cloudflare.json'
    if not path.exists():
        raise ValueError('Run ./friends-vpn setup-portal to configure Cloudflare first')
    return json.loads(read_private(path))


def invite(email):
    if email == cloudflare.DISABLED_EMAIL:
        raise ValueError('Use a reachable email address')
    devices.read()
    changed = cloudflare.change_email(cloudflare_api(), cloudflare_settings(), email, allow=True)

    def unblock(inventory):
        return bool(inventory['blocked_emails'].pop(email, None))

    devices.update(unblock)
    print(f'{"Invited" if changed else "Already invited"} {email}. Share {cloudflare.URL}.')


def deploy_portal():
    devices.read()
    config = cloudflare_settings()
    settings = {'issuer': config['issuer'], 'audience': config['audience'],
                'token': read_private(DIRECTORY / 'cloudflare-tunnel-token').strip()}
    items = manage.objects((NAMESPACE + '/portal',))
    artifact = release.read()
    release.mount(items, artifact)
    manage.apply([item for item in items if item['kind'] != 'Deployment'])
    runtime = [item for item in items if item['kind'] == 'Deployment']
    runtime[0]['spec']['template']['metadata']['annotations'] = {
        'self-hosted/config-sha256': hashlib.sha256(json.dumps(settings, sort_keys=True).encode()).hexdigest()}
    manage.check_admission(runtime)
    release.stage(artifact)
    manage.apply([manage.secret(NAMESPACE, NAMESPACE + '-access', settings)])
    manage.apply(runtime)
    manage.kube('-n', NAMESPACE, 'rollout', 'status', 'deployment/friends-vpn-portal', '--timeout=120s')
    print(f'Friends VPN portal: {cloudflare.URL}')


def command(args):
    if args.command in ('render', 'render-portal'):
        import yaml
        app = NAMESPACE + '/portal' if args.command == 'render-portal' else NAMESPACE
        items = manage.objects((app,))
        release.mount(items, release.read())
        print(yaml.safe_dump_all(items, sort_keys=False))
        return
    if args.command == 'setup-portal':
        config, token = cloudflare.setup(cloudflare_api())
        write_private(DIRECTORY / 'cloudflare.json', json.dumps(config, indent=2) + '\n')
        write_private(DIRECTORY / 'cloudflare-tunnel-token', token + '\n')
        print(f'Configured Cloudflare Access and Tunnel for {cloudflare.URL}.')
        return
    if args.command == 'deploy-portal':
        deploy_portal()
        return
    if args.command == 'invite':
        invite(devices.email_address(args.email))
        return
    if args.command == 'list-users':
        _, policy = cloudflare.read_email_policy(cloudflare_api(), cloudflare_settings())
        emails = sorted(cloudflare.policy_emails(policy) - {cloudflare.DISABLED_EMAIL})
        print('\n'.join(emails) or 'No invited users.')
        return
    if args.command == 'offboard':
        email = devices.email_address(args.email)
        api, config = cloudflare_api(), cloudflare_settings()
        revoke_live(email=email, block=True)
        cloudflare.change_email(api, config, email, allow=False)
        cloudflare.revoke_sessions(api, config, email)
        print(f'Removed {email} from Access and revoked their VPN profiles.')
        return
    if args.command == 'assign':
        assign(args.name, devices.email_address(args.email))
        return
    if args.command == 'status':
        print(manage.kube('-n', NAMESPACE, 'get', 'deploy,pod,svc,networkpolicy', '-o', 'wide').stdout.decode())
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
        deploy(state, args.restore_inventory)
    elif args.command == 'add':
        email = devices.email_address(args.email) if args.email else ''
        if state.get('inventory_migrated'):
            add_live_peer(args.name, email)
        else:
            add_peer(state, args.name, email)
            print('Run ./friends-vpn deploy to activate this client.')
    elif args.command == 'revoke':
        if bool(args.name) == bool(args.email):
            raise ValueError('Enter a device ID or use --email to revoke all its profiles')
        email = devices.email_address(args.email) if args.email else ''
        if not state.get('inventory_migrated'):
            raise ValueError('Run ./friends-vpn deploy once to migrate the gateway before revoking devices')
        revoke_live(args.name, email=email)
        print(f'Revoked {args.name or email}.')
    elif args.command == 'list':
        inventory = devices.read()[0] if state.get('inventory_migrated') else state
        email = devices.email_address(args.email) if args.email else ''
        print('Public endpoint: ' + inventory['endpoint'])
        for identity, peer in sorted(inventory['peers'].items()):
            owner = peer.get('email', '')
            if not email or email == owner:
                print(f'{identity}\t{peer.get("name", identity)}\t{owner or "Unassigned"}\t{", ".join(peer["addresses"])}')
    elif args.command == 'backup':
        inventory, _ = devices.read()
        destination = DIRECTORY / 'backups' / datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')
        destination.mkdir(mode=0o700, parents=True)
        write_private(destination / 'inventory.json', json.dumps(inventory, indent=2) + '\n')
        print(f'Private inventory snapshot: {destination / "inventory.json"}')


def main(argv=None):
    parser = argparse.ArgumentParser(prog='friends-vpn', description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    device_commands = commands.add_parser('device', help='manage WireGuard device profiles').add_subparsers(required=True)
    user_commands = commands.add_parser('user', help='manage the portal email allowlist').add_subparsers(required=True)

    add_device = device_commands.add_parser('add', help='create a private device export')
    add_device.add_argument('name')
    add_device.add_argument('--email')
    add_device.set_defaults(command='add')
    remove_device = device_commands.add_parser('remove', help='revoke one device or all devices for an email')
    remove_device.add_argument('name', nargs='?')
    remove_device.add_argument('--email')
    remove_device.set_defaults(command='revoke')
    list_devices = device_commands.add_parser('list', help='list devices and their owners')
    list_devices.add_argument('--email')
    list_devices.set_defaults(command='list')

    add_user = user_commands.add_parser('add', help='add an exact email to Cloudflare Access')
    add_user.add_argument('email')
    add_user.set_defaults(command='invite')
    remove_user = user_commands.add_parser('remove', help='remove the email, revoke profiles and end Access sessions')
    remove_user.add_argument('email')
    remove_user.set_defaults(command='offboard')
    user_commands.add_parser('list', help='list the emails allowed to sign in').set_defaults(command='list-users')

    init = commands.add_parser('init', help='create a protected WireGuard server identity')
    init.add_argument('--windscribe-profile', required=True)
    init.add_argument('--endpoint', required=True)
    deploy_command = commands.add_parser('deploy', help='install or upgrade the VPN gateway')
    deploy_command.add_argument('--restore-inventory')
    association = commands.add_parser('assign', help='associate an existing device with its owner')
    association.add_argument('name')
    association.add_argument('email')
    commands.add_parser('setup-portal', help='configure Cloudflare Access, Tunnel and DNS')
    commands.add_parser('deploy-portal', help='deploy the website and Tunnel connector')
    for name in ('backup', 'status', 'render', 'render-portal'):
        commands.add_parser(name)
    args = parser.parse_args(argv)
    os.umask(0o077)
    if DIRECTORY.resolve().is_relative_to(manage.ROOT):
        parser.error('Operator storage must be outside Git')
    DIRECTORY.mkdir(mode=0o700, parents=True, exist_ok=True)
    DIRECTORY.chmod(0o700)
    with (DIRECTORY / '.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            command(args)
        except (ValueError, RuntimeError, OSError) as error:
            parser.error(str(error))


if __name__ == '__main__':
    main()
