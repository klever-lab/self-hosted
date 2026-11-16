"""Deploy the application manifests using protected local operator inputs."""

import argparse
import base64
import configparser
from datetime import datetime, timezone
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import tarfile
import urllib.parse
import urllib.request

import yaml

ROOT = Path(__file__).resolve().parents[1]
STATE = Path(os.environ.get('SELF_HOSTED_OPERATOR_DIR', Path.home() / '.local/state/self-hosted')).expanduser().resolve()
GUARANINE = Path(os.environ.get('GUARANINE_OPERATOR_DIR', Path.home() / '.local/state/guaranine-kairos')).expanduser()
APPS = ('qbittorrent', 'calibre-web')
os.environ.setdefault('KUBECTL_REMOTE_COMMAND_WEBSOCKETS', 'false')


def run(args, *, data=None, check=True):
    result = subprocess.run(args, input=data, capture_output=True)
    if check and result.returncode:
        raise RuntimeError(f'{args[0]} failed: {result.stderr.decode().strip()}')
    return result


def kube(*args, data=None, check=True):
    return run(['kubectl', *args], data=data, check=check)


def private_file(path, content):
    with open(path, 'w', opener=lambda name, flags: os.open(name, flags, 0o600)) as output:
        output.write(content)
    path.chmod(0o600)


def load(name):
    path = STATE / name
    if path.stat().st_mode & 0o077:
        raise ValueError(f'{path} must have mode 0600')
    return json.loads(path.read_text())


def apply(items, *, manager='self-hosted'):
    payload = {'apiVersion': 'v1', 'kind': 'List', 'items': items}
    result = kube('apply', '--server-side', '--field-manager=' + manager, '-f', '-', data=json.dumps(payload).encode())
    print(result.stdout.decode(), end='')


def secret(namespace, name, values):
    return {'apiVersion': 'v1', 'kind': 'Secret', 'metadata': {'name': name, 'namespace': namespace},
            'type': 'Opaque', 'data': {k: base64.b64encode(v.encode()).decode() for k, v in values.items()}}


def objects(cfg, selected):
    paths = [ROOT / 'apps' / app for app in selected]
    if set(selected) == set(APPS):
        paths.append(ROOT / 'integrations/books')
    items = []
    for path in paths:
        documents = list(yaml.safe_load_all(kube('kustomize', str(path)).stdout))
        for obj in documents:
            if obj is None:
                continue
            if obj['kind'] == 'Deployment':
                spec = obj['spec']['template']['spec']
            elif obj['kind'] == 'CronJob':
                spec = obj['spec']['jobTemplate']['spec']['template']['spec']
            else:
                spec = None
            if spec is not None:
                spec['nodeSelector'] = {'kubernetes.io/hostname': cfg['node']}
            items.append(obj)
    return items


def enroll(namespace):
    state = json.loads(kube('-n', namespace, 'get', 'secret', 'tailscale-state', '-o', 'json').stdout)
    if state.get('data', {}).get('device_id') or state.get('data', {}).get('authkey'):
        return
    operator = yaml.safe_load((GUARANINE / 'operator.yaml').read_text())
    tailnet = urllib.parse.quote(operator['tailnet'], safe='')

    def api(path, payload=None):
        request = urllib.request.Request('https://api.tailscale.com/api/v2/' + path,
            data=json.dumps(payload).encode() if payload is not None else None,
            headers={'Authorization': 'Bearer ' + operator['tailscale_api_token'],
                     'Content-Type': 'application/json', 'Accept': 'application/json'})
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)

    tag = 'tag:guaranine-app-gateway'
    policy = api(f'tailnet/{tailnet}/acl')
    if tag not in policy.get('tagOwners', {}):
        raise ValueError('Guaranine must provision the application gateway tag first')
    key = api(f'tailnet/{tailnet}/keys', {
        'capabilities': {'devices': {'create': {'reusable': False, 'ephemeral': False,
                         'preauthorized': True, 'tags': [tag]}}},
        'expirySeconds': 3600, 'description': f'self-hosted {namespace} gateway'})
    apply([secret(namespace, 'tailscale-state', {'authkey': key['key']})], manager='self-hosted-enrollment')


def vpn(cfg):
    profile_path = Path(cfg['wireguard_profile']).expanduser().resolve()
    if profile_path.is_relative_to(ROOT):
        raise ValueError('Keep the WireGuard profile outside the repository')
    if profile_path.stat().st_mode & 0o077:
        raise ValueError('The WireGuard profile must have mode 0600')
    profile = configparser.ConfigParser()
    profile.optionxform = str
    profile.read(profile_path)
    host, port = profile['Peer']['Endpoint'].rsplit(':', 1)
    address = cfg.get('wireguard_endpoint_ip') or socket.getaddrinfo(host, int(port), socket.AF_INET, socket.SOCK_DGRAM)[0][4][0]
    if not ipaddress.ip_address(address).is_global:
        raise ValueError('The VPN endpoint must have a public address')
    cfg['wireguard_endpoint_ip'] = address
    private_file(STATE / 'config.json', json.dumps(cfg, indent=2) + '\n')
    peer = {'address': address, 'port': int(port), 'public_key': profile['Peer']['PublicKey'],
            'allowed_ips': ['0.0.0.0/0'], 'persistent_keepalive_interval': 25}
    if 'PresharedKey' in profile['Peer']:
        peer['pre_shared_key'] = profile['Peer']['PresharedKey']
    tunnel_address = next(x.strip() for x in profile['Interface']['Address'].split(',') if ':' not in x)
    dns = profile['Interface'].get('DNS', '1.1.1.1').split(',')[0].strip()
    content = {
        'log': {'level': 'warn'},
        'dns': {'servers': [{'type': 'udp', 'tag': 'vpn-dns', 'server': dns, 'detour': 'vpn'}],
                'strategy': 'ipv4_only'},
        'inbounds': [
            {'type': 'socks', 'tag': 'socks', 'listen': '127.0.0.1', 'listen_port': 1080},
            {'type': 'direct', 'tag': 'dns', 'listen': '127.0.0.1', 'listen_port': 53,
             'override_address': dns, 'override_port': 53},
        ],
        # Flannel over Tailscale gives this cluster 1230-byte pod interfaces.
        # Leave room for the outer WireGuard IPv4/UDP/encryption headers.
        'endpoints': [{'type': 'wireguard', 'tag': 'vpn', 'system': False, 'mtu': 1120,
                       'udp_fragment': True,
                       'address': [tunnel_address], 'private_key': profile['Interface']['PrivateKey'],
                       'peers': [peer]}],
        'route': {'final': 'vpn', 'default_domain_resolver': 'vpn-dns'},
    }
    policy = {'apiVersion': 'networking.k8s.io/v1', 'kind': 'NetworkPolicy',
        'metadata': {'name': 'wireguard-egress', 'namespace': 'qbittorrent'},
        'spec': {'podSelector': {'matchLabels': {'app': 'qbittorrent'}}, 'policyTypes': ['Egress'],
                 'egress': [{'to': [{'ipBlock': {'cidr': address + '/32'}}],
                             'ports': [{'protocol': 'UDP', 'port': int(port)}]}]}}
    return secret('qbittorrent', 'wireguard', {'config.json': json.dumps(content)}), policy


def gateway(cfg, app):
    upstream = 'http://qbittorrent:8080' if app == 'qbittorrent' else 'http://127.0.0.1:8080'
    serve = {'TCP': {'443': {'HTTPS': True}},
             'Web': {'${TS_CERT_DOMAIN}:443': {'Handlers': {'/': {'Proxy': upstream}}}},
             'AllowFunnel': {'${TS_CERT_DOMAIN}:443': False}}
    return {'apiVersion': 'v1', 'kind': 'ConfigMap',
            'metadata': {'name': 'edge-config', 'namespace': app},
            'data': {'hostname': cfg['hostnames'][app], 'serve.json': json.dumps(serve)}}


def deploy(cfg, selected):
    access = load('access.json')
    fingerprints = {}
    items = objects(cfg, selected)
    apply([obj for obj in items if obj['kind'] == 'Namespace'])
    initial = [obj for obj in items if obj['kind'] not in ('Namespace', 'Deployment', 'CronJob')]
    apply(initial)
    for app in selected:
        values = {'username': access[app]['username'], 'password': access[app]['password']}
        if app == 'calibre-web':
            values['import-password'] = access['import-password']
        else:
            values['domain'] = cfg['hostnames'][app] + '.' + cfg['dns_suffix']
        fingerprints[app] = hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()
        apply([secret(app, 'credentials', values)])
        apply([gateway(cfg, app)])
        enroll(app)
    if 'qbittorrent' in selected:
        vpn_items = list(vpn(cfg))
        apply(vpn_items)
        fingerprints['qbittorrent'] = hashlib.sha256((fingerprints['qbittorrent'] + json.dumps(vpn_items, sort_keys=True)).encode()).hexdigest()
    if set(selected) == set(APPS):
        apply([secret('qbittorrent', 'book-importer', {
            'qbt-username': access['qbittorrent']['username'], 'qbt-password': access['qbittorrent']['password'],
            'calibre-password': access['import-password']})])
    runtime = [obj for obj in items if obj['kind'] in ('Deployment', 'CronJob')]
    for obj in runtime:
        if obj['kind'] == 'Deployment':
            template = obj['spec']['template']
        else:
            template = obj['spec']['jobTemplate']['spec']['template']
        template.setdefault('metadata', {}).setdefault('annotations', {})['self-hosted/config-sha256'] = fingerprints[obj['metadata']['namespace']]
    # Admission checks run after namespaces, credentials, and network isolation exist.
    kube('apply', '--server-side', '--field-manager=self-hosted', '--dry-run=server', '-f', '-',
         data=json.dumps({'apiVersion': 'v1', 'kind': 'List', 'items': runtime}).encode())
    apply(runtime)
    for app in selected:
        print(f'{app}: https://{cfg["hostnames"][app]}.{cfg["dns_suffix"]}')
    print('Check ./self-hosted status while images download and the deployments start.')


def backup(cfg):
    """Stop writers and export application state and books outside the worker."""
    destination = STATE / 'backups' / datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    destination.mkdir(mode=0o700, parents=True)
    cron = json.loads(kube('-n', 'qbittorrent', 'get', 'cronjob', 'book-importer', '-o', 'json').stdout)
    was_suspended = cron['spec'].get('suspend', False)
    replicas = {}
    maintenance = []
    try:
        kube('-n', 'qbittorrent', 'patch', 'cronjob', 'book-importer', '--type=merge',
             '-p', json.dumps({'spec': {'suspend': True}}))
        jobs = json.loads(kube('-n', 'qbittorrent', 'get', 'jobs', '-o', 'json').stdout)['items']
        for job in jobs:
            if job['status'].get('active'):
                kube('-n', 'qbittorrent', 'wait', '--for=condition=complete', 'job/' + job['metadata']['name'], '--timeout=120s')
        for app in APPS:
            deployment = json.loads(kube('-n', app, 'get', 'deployment', app, '-o', 'json').stdout)
            replicas[app] = deployment['spec']['replicas']
            kube('-n', app, 'scale', 'deployment/' + app, '--replicas=0')
            kube('-n', app, 'wait', '--for=delete', 'pod', '-l', 'app=' + app, '--timeout=180s')
        for name in ('config.json', 'access.json', 'qbittorrent-wireguard.conf'):
            source = STATE / name if name != 'qbittorrent-wireguard.conf' else Path(cfg['wireguard_profile'])
            private_file(destination / name, source.read_text())
        checksums = {}
        for app in APPS:
            names = ['credentials', 'tailscale-state']
            if app == 'qbittorrent':
                names += ['wireguard', 'book-importer']
            state = kube('-n', app, 'get', 'secret', *names, '-o', 'json').stdout.decode()
            private_file(destination / (app + '-secrets.json'), state)
            volumes = ('config', 'book-import-state') if app == 'qbittorrent' else ('config', 'library')
            pod_name = 'state-backup-' + secrets.token_hex(3)
            pod = {'apiVersion': 'v1', 'kind': 'Pod', 'metadata': {'name': pod_name, 'namespace': app},
                'spec': {'restartPolicy': 'Never', 'automountServiceAccountToken': False,
                    'nodeSelector': {'kubernetes.io/hostname': cfg['node']},
                    'securityContext': {'runAsNonRoot': True, 'runAsUser': 1000, 'runAsGroup': 1000,
                        'fsGroup': 1000, 'supplementalGroupsPolicy': 'Strict', 'seccompProfile': {'type': 'RuntimeDefault'}},
                    'containers': [{'name': 'backup', 'image': 'ghcr.io/bjw-s-labs/calibre-web:0.6.27@sha256:5d08f83d3bb2ca51bcce0ca46c2c2c59aa4bbb6c384317dac7f241e51c976e58',
                        'command': ['sleep', '600'],
                        'securityContext': {'allowPrivilegeEscalation': False, 'readOnlyRootFilesystem': True, 'capabilities': {'drop': ['ALL']}},
                        'resources': {'requests': {'cpu': '25m', 'memory': '32Mi'}, 'limits': {'cpu': '1', 'memory': '128Mi'}},
                        'volumeMounts': [{'name': v, 'mountPath': '/data/' + v, 'readOnly': True} for v in volumes]}],
                    'volumes': [{'name': v, 'persistentVolumeClaim': {'claimName': v, 'readOnly': True}} for v in volumes]}}
            apply([pod])
            maintenance.append((app, pod_name))
            kube('-n', app, 'wait', '--for=condition=Ready', 'pod/' + pod_name, '--timeout=120s')
            archive = destination / (app + '.tar.gz.partial')
            with open(archive, 'wb', opener=lambda name, flags: os.open(name, flags, 0o600)) as output:
                result = subprocess.run(['kubectl', '-n', app, 'exec', pod_name, '--', 'tar', '-czf', '-', '-C', '/data', *volumes],
                                        stdout=output, stderr=subprocess.PIPE)
            if result.returncode:
                raise RuntimeError('State export failed; incomplete archive retained for diagnosis')
            with tarfile.open(archive, 'r:gz') as contents:
                for member in contents:
                    if member.isfile():
                        with contents.extractfile(member) as stream:
                            while stream.read(1024 * 1024):
                                pass
            complete = archive.with_suffix('')
            archive.rename(complete)
            with complete.open('rb') as stream:
                checksums[complete.name] = hashlib.file_digest(stream, 'sha256').hexdigest()
        private_file(destination / 'manifest.json', json.dumps({'sha256': checksums, 'downloads_included': False}, indent=2) + '\n')
    finally:
        restore_errors = []
        for app, name in maintenance:
            result = kube('-n', app, 'delete', 'pod', name, '--ignore-not-found', '--wait=false', check=False)
            if result.returncode:
                restore_errors.append(f'{app}: remove temporary backup pod')
        for app, count in replicas.items():
            result = kube('-n', app, 'scale', 'deployment/' + app, '--replicas=' + str(count), check=False)
            if result.returncode:
                restore_errors.append(f'{app}: restore application replicas')
            elif count:
                result = kube('-n', app, 'rollout', 'status', 'deployment/' + app, '--timeout=180s', check=False)
                if result.returncode:
                    restore_errors.append(f'{app}: wait for restored application readiness')
        if not restore_errors:
            result = kube('-n', 'qbittorrent', 'patch', 'cronjob', 'book-importer', '--type=merge',
                          '-p', json.dumps({'spec': {'suspend': was_suspended}}), check=False)
            if result.returncode:
                restore_errors.append('restore the book import schedule')
        if restore_errors:
            raise RuntimeError('Backup cleanup requires attention: ' + '; '.join(restore_errors))
    print(f'Verified application-state and book-library backup: {destination}')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    init = commands.add_parser('init')
    init.add_argument('--node', required=True)
    init.add_argument('--wireguard-profile', required=True)
    init.add_argument('--username', default='klever')
    for name in ('deploy', 'render', 'status'):
        command = commands.add_parser(name)
        command.add_argument('app', nargs='?', choices=APPS)
    commands.add_parser('credentials')
    commands.add_parser('import-books')
    commands.add_parser('backup')
    args = parser.parse_args()
    if STATE.is_relative_to(ROOT):
        raise ValueError('Operator storage must be outside the checkout')
    STATE.mkdir(mode=0o700, parents=True, exist_ok=True)
    STATE.chmod(0o700)
    if args.command == 'init':
        if (STATE / 'config.json').exists() or (STATE / 'access.json').exists():
            raise ValueError('Operator configuration already exists; edit it deliberately')
        tailnet = json.loads(run(['tailscale', 'status', '--json']).stdout)['CurrentTailnet']
        cfg = {'node': args.node, 'dns_suffix': tailnet['MagicDNSSuffix'],
               'wireguard_profile': str(Path(args.wireguard_profile).expanduser().resolve()),
               'hostnames': {'qbittorrent': 'qbittorrent', 'calibre-web': 'books'}}
        access = {app: {'username': args.username, 'password': secrets.token_urlsafe(24)} for app in APPS}
        access['import-password'] = secrets.token_urlsafe(32)
        private_file(STATE / 'config.json', json.dumps(cfg, indent=2) + '\n')
        private_file(STATE / 'access.json', json.dumps(access, indent=2) + '\n')
        print(f'Created protected configuration and generated credentials in {STATE}')
        return
    cfg = load('config.json')
    selected = (args.app,) if getattr(args, 'app', None) else APPS
    if args.command == 'render':
        print(yaml.safe_dump_all(objects(cfg, selected), sort_keys=False))
    elif args.command == 'deploy':
        deploy(cfg, selected)
    elif args.command == 'status':
        for app in selected:
            print(kube('-n', app, 'get', 'deploy,pod,pvc,cronjob', '-o', 'wide').stdout.decode())
    elif args.command == 'credentials':
        access = load('access.json')
        for app in APPS:
            print(f'{app}: https://{cfg["hostnames"][app]}.{cfg["dns_suffix"]}')
            print(f'  Username: {access[app]["username"]}\n  Password: {access[app]["password"]}')
    elif args.command == 'import-books':
        name = 'book-importer-manual-' + secrets.token_hex(3)
        result = kube('-n', 'qbittorrent', 'create', 'job', '--from=cronjob/book-importer', name)
        print(result.stdout.decode(), end='')
        print(f'Follow with: kubectl -n qbittorrent logs -f job/{name}')
    elif args.command == 'backup':
        backup(cfg)


if __name__ == '__main__':
    try:
        main()
    except (ValueError, RuntimeError) as error:
        raise SystemExit(str(error))
