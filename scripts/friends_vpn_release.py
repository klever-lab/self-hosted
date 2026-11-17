"""Install the pinned public Friends VPN release for the gateway and portal."""

import base64
import hashlib
import json
import re
from urllib.request import urlopen

import manage
from friends_vpn_inventory import NAMESPACE

APP_DIR = manage.ROOT / 'apps/friends-vpn'
ASSET = 'friends-vpn-linux-amd64.gz'
CHUNK_SIZE = 768 * 1024
APP_MOUNT = '/opt/friends-vpn'
RELEASE_MOUNT = '/mnt/friends-vpn-release'
INSTALL = '''set -o pipefail
checksum=$(cat /mnt/friends-vpn-release/part-* | sha256sum)
test "${checksum%% *}" = "$FRIENDS_VPN_RELEASE_SHA256"
cat /mnt/friends-vpn-release/part-* | gzip -dc > /opt/friends-vpn/friends-vpn
chmod 0555 /opt/friends-vpn/friends-vpn
'''


def read():
    release = json.loads((APP_DIR / 'release.json').read_text())
    if (not re.fullmatch(r'friends-vpn-[a-f0-9]{12}', release['tag'])
            or not re.fullmatch(r'[a-f0-9]{64}', release['sha256'])
            or not 0 < release['size'] <= 16 * 1024 * 1024):
        raise ValueError('Invalid Friends VPN release pin')
    return release


def parts(release):
    for index, offset in enumerate(range(0, release['size'], CHUNK_SIZE)):
        part = f'part-{index:02d}'
        yield f'friends-vpn-release-{release["sha256"][:24]}-{index:02d}', part, offset


def stage(release):
    url = f'https://github.com/klever-lab/self-hosted/releases/download/{release["tag"]}/{ASSET}'
    with urlopen(url, timeout=30) as response:
        content = response.read(release['size'] + 1)
    if len(content) != release['size'] or hashlib.sha256(content).hexdigest() != release['sha256']:
        raise ValueError('Friends VPN release size or checksum differs from the pinned value')
    configmaps = []
    for name, _, offset in parts(release):
        data = base64.b64encode(content[offset:offset + CHUNK_SIZE]).decode()
        configmaps.append({'apiVersion': 'v1', 'kind': 'ConfigMap', 'immutable': True,
                           'metadata': {'name': name, 'namespace': NAMESPACE},
                           'binaryData': {'archive-part': data}})
    manage.apply(configmaps)


def mount(items, release):
    sources = [{'configMap': {'name': name, 'items': [{'key': 'archive-part', 'path': part}]}}
               for name, part, _ in parts(release)]
    for deployment in (item for item in items if item['kind'] == 'Deployment'):
        pod = deployment['spec']['template']['spec']
        app = pod['containers'][0]
        pod.setdefault('nodeSelector', {})['kubernetes.io/arch'] = 'amd64'
        pod['volumes'].extend([
            {'name': 'release', 'projected': {'sources': sources}},
            {'name': 'executable', 'emptyDir': {'sizeLimit': '32Mi'}},
        ])
        executable = {'name': 'executable', 'mountPath': APP_MOUNT}
        pod.setdefault('initContainers', []).append({
            'name': 'install-release', 'image': app['image'], 'command': ['/bin/sh', '-euc', INSTALL],
            'env': [{'name': 'FRIENDS_VPN_RELEASE_SHA256', 'value': release['sha256']}],
            'resources': {'requests': {'cpu': '10m', 'memory': '8Mi'}, 'limits': {'memory': '32Mi'}},
            'securityContext': app['securityContext'],
            'volumeMounts': [executable, {'name': 'release', 'mountPath': RELEASE_MOUNT, 'readOnly': True}],
        })
        app['volumeMounts'].append({**executable, 'readOnly': True})
