"""Verify qBittorrent admission using server dry runs; create no workloads."""

from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
import json

import manage

POLICY = 'self-hosted-qbittorrent-security'


def check(pod, name, *, denied=False, ephemeral=False):
    if ephemeral:
        path = f'/api/v1/namespaces/qbittorrent/pods/{pod["metadata"]["name"]}/ephemeralcontainers?dryRun=All&fieldValidation=Strict'
        result = manage.kube('--request-timeout=15s', 'replace', '--raw', path, '-f', '-',
                             data=json.dumps(pod).encode(), check=False)
    else:
        result = manage.dry_run_pod(pod, check=False)
    error = result.stderr.decode()
    rejected = result.returncode != 0 and POLICY in error and 'denied request' in error
    if (denied and not rejected) or (not denied and result.returncode):
        raise RuntimeError(f'{name}: expected {"policy rejection" if denied else "admission"}; {error.strip() or "request was accepted"}')
    print(f'{name}: {"rejected" if denied else "accepted"}')


def main():
    items = manage.objects(manage.APPS)
    pods = list(manage.pod_templates(items))
    for index, pod in enumerate(pods):
        check(pod, f'workload template {index + 1} ({pod["metadata"]["namespace"]})')
    base = next(p for p in pods if p['metadata']['labels'].get('app') == 'qbittorrent')
    cases = []

    def mutate(name, change):
        pod = deepcopy(base)
        change(pod)
        cases.append((pod, name))

    def main_security(pod):
        return pod['spec']['containers'][0]['securityContext']

    def vpn(pod):
        return next(c for c in pod['spec']['initContainers'] if c['name'] == 'vpn')

    for key in ('hostNetwork', 'hostPID', 'hostIPC', 'shareProcessNamespace'):
        mutate(key, lambda p, key=key: p['spec'].update({key: True}))
    mutate('alternate runtime', lambda p: p['spec'].update(runtimeClassName='crun'))
    mutate('hostPath', lambda p: p['spec']['volumes'].append({'name': 'host', 'hostPath': {'path': '/'}}))
    mutate('host port', lambda p: p['spec']['containers'][0].update(ports=[{'containerPort': 8080, 'hostPort': 30080}]))
    mutate('pod root UID', lambda p: p['spec']['securityContext'].update(runAsUser=0))
    mutate('pod root GID', lambda p: p['spec']['securityContext'].update(runAsGroup=0))
    mutate('container root UID', lambda p: main_security(p).update(runAsUser=0))
    mutate('container root GID', lambda p: main_security(p).update(runAsGroup=0))
    mutate('container permits root', lambda p: main_security(p).update(runAsNonRoot=False))
    mutate('privileged container', lambda p: main_security(p).update(privileged=True, allowPrivilegeEscalation=True))
    mutate('privilege escalation', lambda p: main_security(p).update(allowPrivilegeEscalation=True))
    mutate('writable root', lambda p: main_security(p).update(readOnlyRootFilesystem=False))
    mutate('main NET_ADMIN', lambda p: main_security(p)['capabilities'].update(add=['NET_ADMIN']))
    mutate('default capabilities', lambda p: main_security(p).pop('capabilities'))
    mutate('pod unconfined seccomp', lambda p: p['spec']['securityContext'].update(seccompProfile={'type': 'Unconfined'}))
    mutate('container unconfined seccomp', lambda p: main_security(p).update(seccompProfile={'type': 'Unconfined'}))
    mutate('container unconfined AppArmor', lambda p: main_security(p).update(appArmorProfile={'type': 'Unconfined'}))
    mutate('SELinux override', lambda p: main_security(p).update(seLinuxOptions={'type': 'spc_t'}))
    mutate('custom sysctl', lambda p: p['spec']['securityContext'].update(sysctls=[{'name': 'net.ipv4.ip_forward', 'value': '1'}]))
    mutate('legacy AppArmor override', lambda p: p['metadata']['annotations'].update({'container.apparmor.security.beta.kubernetes.io/qbittorrent': 'unconfined'}))
    mutate('host probe', lambda p: p['spec']['containers'][0].update(readinessProbe={'httpGet': {'host': '127.0.0.1', 'port': 8080}}))
    mutate('host lifecycle hook', lambda p: p['spec']['containers'][0].update(lifecycle={'postStart': {'httpGet': {'host': '127.0.0.1', 'port': 8080}}}))
    mutate('untagged image', lambda p: p['spec']['containers'][0].update(image='alpine:latest'))
    mutate('fake VPN image', lambda p: vpn(p).update(image=p['spec']['containers'][0]['image']))
    mutate('renamed VPN sidecar', lambda p: vpn(p).update(name='fake-vpn'))
    mutate('VPN in unrelated workload', lambda p: p['metadata']['labels'].update(app='fake-workload'))
    mutate('VPN command override', lambda p: vpn(p).update(command=['/bin/sh', '-c', 'sleep 3600']))
    mutate('VPN args override', lambda p: vpn(p).update(args=['malicious']))
    mutate('VPN lifecycle command', lambda p: vpn(p).update(lifecycle={'postStart': {'exec': {'command': ['sh', '-c', 'true']}}}))
    mutate('VPN probe command', lambda p: vpn(p)['readinessProbe'].update(exec={'command': ['sh', '-c', 'true']}))
    mutate('VPN preload environment', lambda p: vpn(p)['env'].append({'name': 'LD_PRELOAD', 'value': '/tmp/evil.so'}))
    mutate('VPN command environment', lambda p: vpn(p)['env'].append({'name': 'VPN_UP_COMMAND', 'value': 'sh -c true'}))
    mutate('VPN duplicate environment', lambda p: vpn(p)['env'].append({'name': 'WIREGUARD_IMPLEMENTATION', 'value': 'userspace'}))
    mutate('VPN imported environment', lambda p: vpn(p).update(envFrom=[{'configMapRef': {'name': 'edge-config'}}]))
    mutate('VPN public control server', lambda p: next(e for e in vpn(p)['env'] if e['name'] == 'HTTP_CONTROL_SERVER_ADDRESS').update(value='0.0.0.0:8000'))
    mutate('VPN extra mount', lambda p: vpn(p)['volumeMounts'].append({'name': 'config', 'mountPath': '/application'}))
    mutate('VPN SYS_ADMIN', lambda p: vpn(p)['securityContext']['capabilities']['add'].append('SYS_ADMIN'))
    mutate('VPN privilege escalation', lambda p: vpn(p)['securityContext'].update(allowPrivilegeEscalation=True))
    mutate('root init container', lambda p: p['spec']['initContainers'][0]['securityContext'].update(runAsUser=0))

    # These dry runs are independent. Bound concurrency for a small control plane
    # while avoiding one tailnet round trip per case in series.
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda case: check(case[0], case[1], denied=True), cases))

    live = json.loads(manage.kube('-n', 'qbittorrent', 'get', 'pods', '-l', 'app=qbittorrent', '-o', 'json').stdout)
    pod = next(p for p in live['items'] if not p['metadata'].get('deletionTimestamp'))
    pod['spec'].setdefault('ephemeralContainers', []).append({
        'name': 'self-hosted-security-check', 'image': base['spec']['containers'][0]['image'],
        'command': ['true'], 'securityContext': {
            'runAsNonRoot': True, 'runAsUser': 1000, 'runAsGroup': 1000,
            'allowPrivilegeEscalation': False, 'readOnlyRootFilesystem': True,
            'capabilities': {'drop': ['ALL']}}})
    check(pod, 'restricted ephemeral container', ephemeral=True)
    for key, value in (('runAsUser', 0), ('capabilities', {'drop': ['ALL'], 'add': ['NET_ADMIN']})):
        changed = deepcopy(pod)
        changed['spec']['ephemeralContainers'][-1]['securityContext'][key] = value
        check(changed, f'ephemeral {key} override', denied=True, ephemeral=True)
    print('All admission checks passed; dry runs created no Pods or debug containers.')


if __name__ == '__main__':
    main()
