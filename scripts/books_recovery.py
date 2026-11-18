"""Snapshot the Books volumes and restore onto fresh OpenEBS claims."""

from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
import re
import secrets
import subprocess
import time

import requests

import manage
from book_storage import BookStorage, sha256

APP = 'calibre-web'
IMAGE = 'ghcr.io/home-operations/sabnzbd:5.1.3@sha256:07790a7829201d9ea302e8b862078beac4b1a07003fe8359e3f069b6bcd84a95'
CLAIMS = ('config', 'library')
SEEDING = {'uploading', 'stalledUP', 'queuedUP', 'forcedUP'}
STOPPED = {'stoppedUP', 'pausedUP'}


def kube_json(*args):
    result = manage.kube('--request-timeout=15s', *args, '-o', 'json').stdout
    return json.loads(result) if result else None


def mount_claims(deployment):
    return {volume['name']: volume['persistentVolumeClaim']['claimName']
            for volume in deployment['spec']['template']['spec']['volumes'] if volume['name'] in CLAIMS}


@contextmanager
def pause_seeds(cfg, work):
    auth = manage.load('access.json')['qbittorrent']
    host = cfg['hostnames']['qbittorrent'] + '.' + cfg['dns_suffix']
    if not re.fullmatch(r'[a-z0-9-]+\.[a-z0-9-]+\.ts\.net', host):
        raise ValueError('Expected the private qBittorrent hostname')
    origin = 'https://' + host
    with requests.Session() as session:
        session.trust_env = False
        session.headers.update({'Origin': origin, 'Referer': origin + '/'})

        def api(endpoint, *, data=None):
            url = origin + '/api/v2/' + endpoint
            method = 'POST' if data is not None else 'GET'
            response = session.request(method, url, data=data, timeout=15, allow_redirects=False)
            if response.status_code not in (200, 204):
                raise RuntimeError(f'qBittorrent {endpoint} returned HTTP {response.status_code}')
            return response

        api('auth/login', data={'username': auth['username'], 'password': auth['password']})
        if not session.cookies:
            raise RuntimeError('qBittorrent did not authenticate')
        selected = {t['hash']: t['state'] for t in api('torrents/info').json() if t['state'] in SEEDING}
        hashes = '|'.join(selected)
        if not selected:
            yield
            return

        def wait_states(expected):
            deadline = time.monotonic() + 30
            while True:
                states = {t['hash']: t['state'] for t in api('torrents/info').json()}
                if all(expected(states.get(h, ''), initial) for h, initial in selected.items()):
                    return
                if time.monotonic() >= deadline:
                    raise RuntimeError('qBittorrent did not confirm all requested torrent states')
                time.sleep(1)

        recovery = work / 'paused-seeds.json'
        manage.private_file(recovery, json.dumps(selected))
        try:
            api('torrents/stop', data={'hashes': hashes})
            wait_states(lambda state, _: state in STOPPED)
            print(f'Paused {len(selected)} qBittorrent seeds for the export and upload.', flush=True)
            yield
        finally:
            normal = '|'.join(h for h, state in selected.items() if state != 'forcedUP')
            forced = '|'.join(h for h, state in selected.items() if state == 'forcedUP')
            if normal:
                api('torrents/start', data={'hashes': normal})
            if forced:
                api('torrents/setForceStart', data={'hashes': forced, 'value': 'true'})
            wait_states(lambda state, initial: state == 'forcedUP' if initial == 'forcedUP' else state in SEEDING)
            recovery.unlink()
            print('Resumed the qBittorrent seeds.', flush=True)


@contextmanager
def maintenance(namespace, claims, *, worker=None, tools=None):
    name = 'books-maintenance-' + secrets.token_hex(4)
    labels = {'app': 'books-restore' if tools else 'books-backup'}
    container = {
        'name': 'books', 'image': IMAGE, 'command': ['sleep', '7200'],
        'securityContext': {'allowPrivilegeEscalation': False, 'readOnlyRootFilesystem': True,
                            'capabilities': {'drop': ['ALL']}},
        'resources': {'requests': {'cpu': '25m', 'memory': '32Mi'}, 'limits': {'cpu': '1', 'memory': '512Mi'}},
        'volumeMounts': [{'name': volume, 'mountPath': '/data/' + volume, 'readOnly': not tools} for volume in claims],
    }
    spec = {
        'restartPolicy': 'Never', 'automountServiceAccountToken': False, 'activeDeadlineSeconds': 7200,
        'securityContext': {'runAsNonRoot': True, 'runAsUser': 1000, 'runAsGroup': 1000, 'fsGroup': 1000,
                            'fsGroupChangePolicy': 'OnRootMismatch', 'supplementalGroupsPolicy': 'Strict',
                            'seccompProfile': {'type': 'RuntimeDefault'}},
        'containers': [container],
        'volumes': [{'name': volume, 'persistentVolumeClaim': {'claimName': claim, 'readOnly': not tools}}
                    for volume, claim in claims.items()],
    }
    if worker:
        spec['nodeSelector'] = {'kubernetes.io/hostname': worker}
    if tools:
        spec['volumes'].extend([
            {'name': 'tools', 'configMap': {'name': tools}},
            {'name': 'storage', 'secret': {'secretName': 'books-s3', 'defaultMode': 0o440}},
            {'name': 'tmp', 'emptyDir': {'sizeLimit': '22Gi'}},
        ])
        container['volumeMounts'].extend([
            {'name': 'tools', 'mountPath': '/scripts', 'readOnly': True},
            {'name': 'storage', 'mountPath': '/storage', 'readOnly': True},
            {'name': 'tmp', 'mountPath': '/tmp'},
        ])
    pod = {'apiVersion': 'v1', 'kind': 'Pod', 'metadata': {'name': name, 'namespace': namespace, 'labels': labels},
           'spec': spec}
    manage.dry_run_pod(pod)
    manage.apply([pod])
    try:
        manage.kube('-n', namespace, 'wait', '--for=condition=Ready', 'pod/' + name, '--timeout=240s')
        yield name
    finally:
        manage.kube('-n', namespace, 'delete', 'pod', name, '--ignore-not-found', '--wait=false')


@contextmanager
def books_quiet(leave_suspended=()):
    """Restore writer and importer settings on exit, including failed backups."""
    paused = []
    old_count = None
    completed = False
    try:
        for namespace in ('qbittorrent', 'sabnzbd'):
            cron = kube_json('-n', namespace, 'get', 'cronjob', 'book-importer', '--ignore-not-found')
            if cron and not cron['spec'].get('suspend'):
                paused.append(namespace)
                manage.kube('-n', namespace, 'patch', 'cronjob', 'book-importer',
                            '--type=merge', '-p', '{"spec":{"suspend":true}}')
            jobs = kube_json('-n', namespace, 'get', 'jobs')
            for job in jobs['items']:
                if job['status'].get('active') and job['metadata']['name'].startswith('book-importer-'):
                    manage.kube('-n', namespace, 'wait', '--for=condition=complete',
                                'job/' + job['metadata']['name'], '--timeout=600s')
        deployment = kube_json('-n', APP, 'get', 'deployment', APP)
        old_count = deployment['spec']['replicas']
        if old_count:
            manage.kube('-n', APP, 'scale', 'deployment/' + APP, '--replicas=0')
            manage.kube('-n', APP, 'wait', '--for=delete', 'pod', '-l', 'app=' + APP, '--timeout=180s')
        yield deployment
        completed = True
    finally:
        if old_count:
            manage.kube('-n', APP, 'scale', 'deployment/' + APP, '--replicas=' + str(old_count))
            manage.kube('-n', APP, 'rollout', 'status', 'deployment/' + APP, '--timeout=240s')
        for namespace in paused:
            if not completed or namespace not in leave_suspended:
                manage.kube('-n', namespace, 'patch', 'cronjob', 'book-importer',
                            '--type=merge', '-p', '{"spec":{"suspend":false}}')


def legacy_receipts():
    rows = []
    for namespace in ('qbittorrent', 'sabnzbd'):
        if not kube_json('-n', namespace, 'get', 'pvc', 'book-import-state', '--ignore-not-found'):
            continue
        with maintenance(namespace, {'state': 'book-import-state'}) as pod:
            result = manage.kube('-n', namespace, 'exec', pod, '--', 'python3', '-c', """
import json
from pathlib import Path
import sqlite3
path = Path('/data/state/imports.sqlite3')
if path.is_file():
    with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True) as database:
        print(json.dumps(database.execute('SELECT sha256, status, book_id FROM imports').fetchall()))
else:
    print('[]')
""")
            rows.extend(json.loads(result.stdout))
    return rows


def backup(cfg, storage, deployment):
    work = manage.STATE / 'books-backups' / datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    work.mkdir(parents=True, mode=0o700)
    claims = mount_claims(deployment)
    if 'inbox-tmp' in {vol['name'] for vol in deployment['spec']['template']['spec']['volumes']}:
        receipts = []
    else:
        receipts = legacy_receipts()
    archive = work / 'books.tar.gz'
    with pause_seeds(cfg, work), maintenance(APP, claims) as pod:
        counts = manage.kube('-n', APP, 'exec', pod, '--', 'python3', '-c', """
import json
import sqlite3
counts = []
for path, table in (('library/metadata.db', 'books'), ('config/app.db', 'user')):
    with sqlite3.connect('file:/data/' + path + '?mode=ro', uri=True) as database:
        if database.execute('PRAGMA quick_check').fetchall() != [('ok',)]:
            raise RuntimeError('Source Books database integrity check failed')
        counts.append(database.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0])
print(json.dumps(counts))
""").stdout
        print('Exporting the stopped Calibre-Web volumes.', flush=True)
        with open(archive, 'xb', opener=lambda name, flags: os.open(name, flags, 0o600)) as output:
            result = subprocess.run(['kubectl', '-n', APP, 'exec', pod, '--', 'tar', '-czf', '-',
                                     '-C', '/data', *CLAIMS], stdout=output, timeout=5400)
        if result.returncode or not archive.stat().st_size:
            raise RuntimeError('Books archive export failed')
        digest = sha256(archive)
        name = work.name + '-' + digest[:12]
        manifest = work / 'manifest.json'
        manage.private_file(manifest, json.dumps({'archive_sha256': digest, 'counts': json.loads(counts),
                                                  'import_receipts': receipts}) + '\n')
        print(f'Uploading {archive.stat().st_size / 1024**2:.0f} MiB of Books state to OVH.', flush=True)
        storage.put('backups/' + name + '.tar.gz', archive, digest)
        storage.put('backups/' + name + '.json', manifest, sha256(manifest))
    print(f'OVH Books snapshot {name}', flush=True)
    return name


def restore(cfg, storage_config, worker, snapshot):
    cfg = {**cfg, 'calibre_worker': worker}
    node = kube_json('get', 'node', worker)
    if not any(c['type'] == 'Ready' and c['status'] == 'True' for c in node['status']['conditions']):
        raise RuntimeError('The Books restore worker is not Ready')
    items = manage.objects((APP,), cfg)
    deployment = next(obj for obj in items if obj['kind'] == 'Deployment' and obj['metadata']['name'] == APP)
    tools = next(obj for obj in items if obj['kind'] == 'ConfigMap' and obj['metadata']['name'].startswith('books-tools-'))
    claims = [obj for obj in items if obj['kind'] == 'PersistentVolumeClaim']
    network = next(obj for obj in items if obj['kind'] == 'NetworkPolicy' and obj['metadata']['name'] == APP)
    egress = network['spec']['egress']
    policy = {'apiVersion': 'networking.k8s.io/v1', 'kind': 'NetworkPolicy',
              'metadata': {'name': 'books-restore', 'namespace': APP},
              'spec': {'podSelector': {'matchLabels': {'app': 'books-restore'}},
                       'policyTypes': ['Ingress', 'Egress'], 'egress': egress}}
    manage.apply([tools, *claims, policy, manage.secret(APP, 'books-s3', {'credentials.json': json.dumps(storage_config)})])
    try:
        with maintenance(APP, mount_claims(deployment), worker=worker, tools=tools['metadata']['name']) as pod:
            result = manage.kube('-n', APP, 'exec', pod, '--', 'python3', '-B', '/scripts/restore_books.py', snapshot)
            print(result.stdout.decode(), end='', flush=True)
    finally:
        manage.kube('-n', APP, 'delete', 'networkpolicy', 'books-restore', '--ignore-not-found')
    return cfg


def stage_usenet(items, storage_config, credentials):
    """Copy completed jobs off the retained downloads claim before using scratch space."""
    namespace = 'sabnzbd'
    live = kube_json('-n', namespace, 'get', 'deployment', namespace, '--ignore-not-found')
    if not live:
        return
    legacy = next(vol.get('persistentVolumeClaim') for vol in live['spec']['template']['spec']['volumes']
                  if vol['name'] == 'downloads')
    if not legacy:
        return
    deployment = next(obj for obj in items if obj['kind'] == 'Deployment' and obj['metadata']['name'] == namespace)
    template = deployment['spec']['template']['spec']
    uploader = next(c for c in template['containers'] if c['name'] == 'books-inbox')
    container = {**uploader, 'command': ['python3', '-u', '/scripts/import_books.py', 'usenet-stage'],
                 'env': [{'name': 'SABNZBD_URL', 'value': 'http://sabnzbd:8080'},
                         {'name': 'PYTHONDONTWRITEBYTECODE', 'value': '1'}]}
    name = 'books-stage-' + secrets.token_hex(4)
    labels = {'app': 'book-importer', 'books-stage': 'true'}
    mounts = {mount['name'] for mount in container['volumeMounts']}
    volumes = [vol for vol in template['volumes'] if vol['name'] in mounts - {'downloads', 'book-credentials'}]
    volumes.extend([
        {'name': 'downloads', 'persistentVolumeClaim': {**legacy, 'readOnly': True}},
        {'name': 'book-credentials', 'secret': {'secretName': name, 'defaultMode': 0o440}},
    ])
    pod = {'apiVersion': 'v1', 'kind': 'Pod', 'metadata': {'name': name, 'namespace': namespace, 'labels': labels},
           'spec': {'automountServiceAccountToken': False, 'activeDeadlineSeconds': 3600,
                    'restartPolicy': 'Never', 'securityContext': template['securityContext'],
                    'containers': [container], 'volumes': volumes}}
    net = next(obj for obj in items if obj['kind'] == 'NetworkPolicy' and obj['metadata']['name'] == namespace)
    ports = [{'protocol': 'TCP', 'port': 8080}]
    policy = {'apiVersion': 'networking.k8s.io/v1', 'kind': 'NetworkPolicy',
              'metadata': {'name': name, 'namespace': namespace},
              'spec': {'podSelector': {'matchLabels': labels}, 'policyTypes': ['Ingress', 'Egress'],
                       'egress': [*net['spec']['egress'], {'to': [
                           {'podSelector': {'matchLabels': {'app': namespace}}}], 'ports': ports}]}}
    tools = next(obj for obj in items if obj['kind'] == 'ConfigMap' and obj['metadata']['namespace'] == namespace
                 and obj['metadata']['name'].startswith('books-tools-'))
    manage.dry_run_pod(pod)
    try:
        manage.apply([tools, policy, manage.secret(namespace, name, credentials),
                      manage.secret(namespace, 'books-s3', {'credentials.json': json.dumps(storage_config)})])
        manage.apply([pod])
        deadline = time.monotonic() + 3600
        while True:
            phase = kube_json('-n', namespace, 'get', 'pod', name)['status']['phase']
            if phase in ('Succeeded', 'Failed'):
                output = manage.kube('-n', namespace, 'logs', name).stdout.decode()
                print(output, end='', flush=True)
                if phase == 'Failed':
                    raise RuntimeError('Usenet staging failed. Retained downloads are still mounted.')
                return
            if time.monotonic() >= deadline:
                raise RuntimeError('Timed out staging retained Usenet downloads')
            time.sleep(3)
    finally:
        manage.kube('-n', namespace, 'delete', 'pod,networkpolicy,secret', name, '--ignore-not-found', '--wait=false')


def storage_client():
    config = manage.books_storage_config()
    storage = BookStorage(config)
    next(storage.objects('backups/'), None)
    return storage, config


def backup_books(cfg):
    storage, _ = storage_client()
    with books_quiet() as deployment:
        return backup(cfg, storage, deployment)


def deploy_torrent_publisher(storage_config):
    namespace = 'qbittorrent'
    access = manage.load('access.json')[namespace]
    items = manage.objects((namespace,))
    publisher = [obj for obj in items if
                 (obj['kind'] == 'CronJob' and obj['metadata']['name'] == 'book-importer') or
                 (obj['kind'] == 'NetworkPolicy' and obj['metadata']['name'] == 'book-importer') or
                 (obj['kind'] == 'ConfigMap' and obj['metadata']['name'].startswith('books-tools-'))]
    manage.check_admission(publisher)
    manage.apply([*publisher, manage.secret(namespace, 'book-importer', {
        'qbt-username': access['username'], 'qbt-password': access['password']}),
        manage.secret(namespace, 'books-s3', {'credentials.json': json.dumps(storage_config)})])


def move_books(cfg, worker):
    storage, storage_config = storage_client()
    deploy_torrent_publisher(storage_config)
    with books_quiet(leave_suspended=('sabnzbd',)) as deployment:
        # Hold the original writer stopped between backup and restore so the
        # tested snapshot contains the exact state at the switch.
        current = mount_claims(deployment)
        if current == {volume: volume + '-' + worker for volume in CLAIMS}:
            raise ValueError('Books already uses this worker. Choose another destination.')
        name = backup(cfg, storage, deployment)
        updated = restore(cfg, storage_config, worker, name)
        revision = deployment['metadata']['annotations']['deployment.kubernetes.io/revision']
        try:
            manage.deploy(updated, (APP,))
            manage.kube('-n', APP, 'rollout', 'status', 'deployment/' + APP, '--timeout=300s')
            running = kube_json('-n', APP, 'get', 'pods', '-l', 'app=' + APP)['items']
            if not any(p['spec'].get('nodeName') == worker and
                       any(c['type'] == 'Ready' and c['status'] == 'True'
                           for c in p.get('status', {}).get('conditions', [])) for p in running):
                raise RuntimeError('Books did not become Ready on the restore worker')
            manage.private_file(manage.STATE / 'config.json', json.dumps(updated, indent=2) + '\n')
        except Exception:
            manage.kube('-n', APP, 'rollout', 'undo', 'deployment/' + APP, '--to-revision=' + revision)
            # Deployment undo restores the Pod template only. Old importers
            # need the old ingress policy if the move failed.
            old_network = next(obj for obj in manage.objects((APP,), cfg)
                               if obj['kind'] == 'NetworkPolicy' and obj['metadata']['name'] == APP)
            old_network['spec']['ingress'][0]['from'].append({
                'namespaceSelector': {'matchLabels': {'kubernetes.io/metadata.name': 'qbittorrent'}},
                'podSelector': {'matchLabels': {'app': 'book-importer'}},
            })
            manage.apply([old_network])
            raise
