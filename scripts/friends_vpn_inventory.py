"""Read and update the shared device inventory with an API version check."""

import base64
import json
import re
import time
from uuid import uuid4

import manage

NAMESPACE = 'friends-vpn'
SECRET = 'friends-vpn-peers'
STATUS = 'friends-vpn-active'
EMAIL = r'[a-z0-9._%+\-]+@[a-z0-9.\-]+\.[a-z0-9\-]{2,63}'


def email_address(value):
    value = value.strip().lower()
    if len(value) > 254 or not re.fullmatch(EMAIL, value):
        raise ValueError('Enter an exact email address')
    return value


def addresses(number):
    if not 2 <= number <= 254:
        raise ValueError('This VPN supports at most 253 allocated client addresses')
    return [f'10.76.0.{number}/32', f'fd76:6672:6965::{number:x}/128']


def read(*, optional=False):
    output = manage.kube('--request-timeout=8s', '-n', NAMESPACE, 'get', 'secret', SECRET,
                         '--ignore-not-found', '-o', 'json').stdout
    if not output:
        if optional:
            return None, None
        raise ValueError('The shared Friends VPN inventory is missing. See the recovery procedure.')
    secret = json.loads(output)
    return json.loads(base64.b64decode(secret['data']['inventory.json'])), secret['metadata']['resourceVersion']


def seed(inventory):
    obj = manage.secret(NAMESPACE, SECRET, {'inventory.json': json.dumps(inventory)})
    result = manage.kube('--request-timeout=8s', 'create', '-f', '-', data=json.dumps(obj).encode(), check=False)
    if result.returncode:
        raise RuntimeError('Could not create the shared Friends VPN inventory. Check the namespace and Secret.')


def update(operation):
    for _ in range(6):
        inventory, version = read()
        if not operation(inventory):
            return inventory
        inventory['revision'] = uuid4().hex
        obj = manage.secret(NAMESPACE, SECRET, {'inventory.json': json.dumps(inventory)})
        obj['metadata']['resourceVersion'] = version
        result = manage.kube('--request-timeout=8s', 'replace', '-f', '-',
                             data=json.dumps(obj).encode(), check=False)
        if not result.returncode:
            return inventory
        if b'(Conflict)' not in result.stderr:
            raise RuntimeError('Could not update the shared Friends VPN inventory. Retry the same command.')
    raise RuntimeError('The Friends VPN inventory kept changing. Retry the same command.')


def wait_for_gateway(still_expected):
    deadline = time.monotonic() + 25
    while time.monotonic() < deadline:
        current, _ = read()
        if not still_expected(current):
            raise RuntimeError('Another operation superseded this device change')
        applied = manage.kube('--request-timeout=8s', '-n', NAMESPACE, 'get', 'configmap', STATUS,
                              '-o', 'jsonpath={.data.revision}').stdout.decode()
        if current['revision'] == applied:
            return
        time.sleep(1)
    raise RuntimeError('The change is saved. Gateway confirmation is pending. Retry the same command.')


def retire(inventory, identity):
    peer = inventory['peers'].pop(identity)
    inventory['retired'][identity] = peer.get('email', '')
