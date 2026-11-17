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
import time
import urllib.parse
import urllib.request

import yaml

ROOT = Path(__file__).resolve().parents[1]
STATE = Path(os.environ.get('SELF_HOSTED_OPERATOR_DIR', Path.home() / '.local/state/self-hosted')).expanduser().resolve()
GUARANINE = Path(os.environ.get('GUARANINE_OPERATOR_DIR', Path.home() / '.local/state/guaranine-kairos')).expanduser()
APPS = ('qbittorrent', 'calibre-web')
ADMISSION_KINDS = ('ValidatingAdmissionPolicy', 'ValidatingAdmissionPolicyBinding')
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


def objects(selected):
    paths = [ROOT / 'apps' / app for app in selected]
    if set(selected) == set(APPS):
        paths.append(ROOT / 'integrations/books')
    items = []
    for path in paths:
        documents = list(yaml.safe_load_all(kube('kustomize', str(path)).stdout))
        for obj in documents:
            if obj is None:
                continue
            # Kustomize's scope table does not yet recognize these cluster resources.
            if obj['kind'] in ADMISSION_KINDS:
                obj['metadata'].pop('namespace', None)
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
    peer_port = cfg.get('qbittorrent_forwarded_port', 6881)
    # 8000 and 9999 are Gluetun's loopback control/health servers, and 8080 is the WebUI.
    if type(peer_port) is not int or not 1024 <= peer_port <= 65535 or peer_port in (8000, 8080, 9999):
        raise ValueError('qbittorrent_forwarded_port must be an available port from 1024 to 65535')
    profile_path = Path(cfg['wireguard_profile']).expanduser().resolve()
    if profile_path.is_relative_to(ROOT):
        raise ValueError('Keep the WireGuard profile outside the repository')
    if profile_path.stat().st_mode & 0o077:
        raise ValueError('The WireGuard profile must have mode 0600')
    profile = configparser.ConfigParser()
    profile.optionxform = str
    profile.read(profile_path)
    endpoint = profile['Peer']['Endpoint']
    host, port = endpoint.rsplit(':', 1)
    # Refresh the resolved IP when the endpoint or forwarded port changes so Windscribe
    # establishes a fresh WireGuard session instead of keeping a pre-forward peer state.
    forwarded = cfg.get('qbittorrent_forwarded_port')
    port_changed = cfg.get('wireguard_forwarded_port') != forwarded
    cached = cfg.get('wireguard_endpoint_ip') if cfg.get('wireguard_endpoint') == endpoint and not port_changed else None
    if cached:
        address = cached
    else:
        candidates = [item[4][0] for item in socket.getaddrinfo(host, int(port), socket.AF_INET, socket.SOCK_DGRAM)]
        previous_ip = cfg.get('wireguard_endpoint_ip') if cfg.get('wireguard_endpoint') == endpoint and port_changed else None
        address = next((ip for ip in candidates if ip != previous_ip), candidates[0])
    if not ipaddress.ip_address(address).is_global:
        raise ValueError('The VPN endpoint must have a public address')
    cfg['wireguard_endpoint'] = endpoint
    cfg['wireguard_endpoint_ip'] = address
    if forwarded is None:
        cfg.pop('wireguard_forwarded_port', None)
    else:
        cfg['wireguard_forwarded_port'] = forwarded
    private_file(STATE / 'config.json', json.dumps(cfg, indent=2) + '\n')
    tunnel_address = next(x.strip() for x in profile['Interface']['Address'].split(',') if ':' not in x)
    lines = [
        '[Interface]',
        'PrivateKey = ' + profile['Interface']['PrivateKey'].strip(),
        'Address = ' + tunnel_address,
        '',
        '[Peer]',
        'PublicKey = ' + profile['Peer']['PublicKey'].strip(),
    ]
    if 'PresharedKey' in profile['Peer']:
        lines.append('PresharedKey = ' + profile['Peer']['PresharedKey'].strip())
    lines.append(f'Endpoint = {address}:{int(port)}')
    vpn_input_ports = str(peer_port) if 'qbittorrent_forwarded_port' in cfg else ''
    policy = {'apiVersion': 'networking.k8s.io/v1', 'kind': 'NetworkPolicy',
        'metadata': {'name': 'wireguard-egress', 'namespace': 'qbittorrent'},
        'spec': {'podSelector': {'matchLabels': {'app': 'qbittorrent'}}, 'policyTypes': ['Egress'],
                 'egress': [{'to': [{'ipBlock': {'cidr': address + '/32'}}],
                             'ports': [{'protocol': 'UDP', 'port': int(port)}]}]}}
    return secret('qbittorrent', 'wireguard', {
        'wg0.conf': '\n'.join(lines) + '\n',
        'peer-port': str(peer_port),
        'vpn-input-ports': vpn_input_ports,
    }), policy


def gateway(cfg, app):
    upstream = 'http://qbittorrent:8080' if app == 'qbittorrent' else 'http://127.0.0.1:8080'
    serve = {'TCP': {'443': {'HTTPS': True}},
             'Web': {'${TS_CERT_DOMAIN}:443': {'Handlers': {'/': {'Proxy': upstream}}}},
             'AllowFunnel': {'${TS_CERT_DOMAIN}:443': False}}
    return {'apiVersion': 'v1', 'kind': 'ConfigMap',
            'metadata': {'name': 'edge-config', 'namespace': app},
            'data': {'hostname': cfg['hostnames'][app], 'serve.json': json.dumps(serve)}}


def install_admission(items):
    """Install and type-check guards before relaxing built-in namespace admission."""
    policies = [obj for obj in items if obj['kind'] == 'ValidatingAdmissionPolicy']
    if any(obj['kind'] == 'Namespace' and obj['metadata']['name'] == 'qbittorrent' for obj in items):
        if (not any(p['metadata']['name'] == 'self-hosted-qbittorrent-security' for p in policies) or
                not any(obj['kind'] == 'ValidatingAdmissionPolicyBinding' and
                        obj['spec']['policyName'] == 'self-hosted-qbittorrent-security' and
                        'Deny' in obj['spec']['validationActions'] for obj in items)):
            raise ValueError('qBittorrent requires its enforcing admission policy and binding')
    if not policies:
        return
    apply(policies)
    for policy in policies:
        name = policy['metadata']['name']
        deadline = time.monotonic() + 30
        while True:
            current = json.loads(kube('--request-timeout=15s', 'get', 'validatingadmissionpolicy', name, '-o', 'json').stdout)
            status = current.get('status', {})
            if status.get('observedGeneration') == current['metadata']['generation'] and 'typeChecking' in status:
                warnings = status['typeChecking'].get('expressionWarnings', [])
                if warnings:
                    raise ValueError(f'Admission policy {name} has type errors: {warnings}')
                break
            if time.monotonic() >= deadline:
                raise RuntimeError(f'Admission policy {name} did not finish type checking')
            time.sleep(0.5)
    apply([obj for obj in items if obj['kind'] == 'ValidatingAdmissionPolicyBinding'])


def pod_templates(items):
    """Exercise Pod admission as well as controller admission during deployment."""
    for obj in items:
        if obj['kind'] == 'Deployment':
            template = obj['spec']['template']
        elif obj['kind'] == 'CronJob':
            template = obj['spec']['jobTemplate']['spec']['template']
        else:
            continue
        yield {'apiVersion': 'v1', 'kind': 'Pod',
               'metadata': {'generateName': 'self-hosted-admission-',
                            'namespace': obj['metadata']['namespace'],
                            'labels': template.get('metadata', {}).get('labels', {}),
                            'annotations': template.get('metadata', {}).get('annotations', {})},
               'spec': template['spec']}


def dry_run_pod(pod, *, check=True):
    # Submit directly to avoid kubectl downloading the full OpenAPI schema over
    # the tailnet. The API server still performs strict validation and admission.
    namespace = urllib.parse.quote(pod['metadata']['namespace'], safe='')
    path = f'/api/v1/namespaces/{namespace}/pods?dryRun=All&fieldValidation=Strict'
    return kube('--request-timeout=15s', 'create', '--raw', path, '-f', '-',
                data=json.dumps(pod).encode(), check=check)


def check_admission_guard(items):
    if any(obj['kind'] == 'ValidatingAdmissionPolicy' for obj in items):
        # A successful positive dry run alone cannot prove the binding is active.
        template = next(p for p in pod_templates(items) if p['metadata']['namespace'] == 'qbittorrent')
        probe = {'apiVersion': 'v1', 'kind': 'Pod',
                 'metadata': {'generateName': 'self-hosted-admission-', 'namespace': 'qbittorrent'},
                 'spec': {'automountServiceAccountToken': False,
                          'securityContext': {'runAsNonRoot': True, 'runAsUser': 1000, 'runAsGroup': 1000,
                                              'seccompProfile': {'type': 'RuntimeDefault'}},
                          'containers': [{'name': 'probe', 'image': template['spec']['containers'][0]['image'],
                                          'securityContext': {'allowPrivilegeEscalation': False,
                                                              'readOnlyRootFilesystem': False,
                                                              'capabilities': {'drop': ['ALL']}}}]}}
        # A new namespace starts restricted. The writable-root probe passes that
        # built-in profile but must fail our policy before its VPN exception opens.
        current = kube('--request-timeout=15s', 'get', 'namespace', 'qbittorrent', '--ignore-not-found', '-o', 'json').stdout
        if not current:
            namespace = json.loads(json.dumps(next(obj for obj in items
                if obj['kind'] == 'Namespace' and obj['metadata']['name'] == 'qbittorrent')))
            namespace['metadata'].setdefault('labels', {})['pod-security.kubernetes.io/enforce'] = 'restricted'
            apply([namespace])
            kube('-n', 'qbittorrent', 'wait', '--for=create', 'serviceaccount/default', '--timeout=30s')
        deadline = time.monotonic() + 30
        while True:
            result = dry_run_pod(probe, check=False)
            error = result.stderr.decode()
            if result.returncode and "self-hosted-qbittorrent-security" in error and 'denied request' in error:
                break
            if result.returncode:
                raise RuntimeError(f'Admission probe failed unexpectedly: {error.strip()}')
            if time.monotonic() >= deadline:
                raise RuntimeError('qBittorrent admission binding did not reject the unsafe probe')
            time.sleep(0.5)


def check_admission(items):
    for pod in pod_templates(items):
        dry_run_pod(pod)


def deploy(cfg, selected):
    access = load('access.json')
    fingerprints = {}
    items = objects(selected)
    install_admission(items)
    check_admission_guard(items)
    apply([obj for obj in items if obj['kind'] == 'Namespace'])
    initial = [obj for obj in items if obj['kind'] not in ('Namespace', 'Deployment', 'CronJob', *ADMISSION_KINDS)]
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
    check_admission(items)
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
    init.add_argument('--wireguard-profile', required=True)
    init.add_argument('--username', default='klever')
    for name in ('deploy', 'render', 'status'):
        command = commands.add_parser(name)
        command.add_argument('app', nargs='?', choices=APPS)
    commands.add_parser('credentials')
    commands.add_parser('import-books')
    gutenberg = commands.add_parser('import-gutenberg')
    gutenberg.add_argument('--limit', type=int, default=200)
    gutenberg.add_argument('--dry-run', action='store_true')
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
        cfg = {'dns_suffix': tailnet['MagicDNSSuffix'],
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
        print(yaml.safe_dump_all(objects(selected), sort_keys=False))
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
    elif args.command == 'import-gutenberg':
        from gutenberg import import_books
        import_books(cfg, load('access.json'), STATE, ROOT,
                     limit=args.limit, dry_run=args.dry_run)
    elif args.command == 'backup':
        backup(cfg)


if __name__ == '__main__':
    try:
        main()
    except (ValueError, RuntimeError) as error:
        raise SystemExit(str(error))
