"""Exercise the shipped initializer with synthetic peers in private namespaces."""
from contextlib import ExitStack
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import unittest

SCRIPT = Path(__file__).resolve()
NETWORK = SCRIPT.parents[1] / 'apps/qbittorrent/network.sh'


def run(*args, **kwargs):
    result = subprocess.run(args, capture_output=True, text=True, **kwargs)
    if result.returncode:
        raise RuntimeError(f'{args[0]} failed: {result.stderr.strip()}')
    return result.stdout


def echo(address, port, *, report_peer=False):
    for kind in (socket.SOCK_STREAM, socket.SOCK_DGRAM):
        listener = socket.socket(socket.AF_INET, kind)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((address, port))
        if kind == socket.SOCK_STREAM:
            listener.listen()

        def serve(listener=listener, kind=kind):
            with listener:
                while True:
                    if kind == socket.SOCK_STREAM:
                        connection, peer = listener.accept()
                        with connection:
                            data = connection.recv(128)
                            connection.sendall(peer[0].encode() if report_peer else data)
                    else:
                        data, peer = listener.recvfrom(128)
                        listener.sendto(peer[0].encode() if report_peer else data, peer)

        threading.Thread(target=serve, daemon=True).start()


def probe(address, port, protocol, source='', device=''):
    # No running application process receives network-administration capabilities.
    os.setgroups([])
    os.setgid(1000)
    os.setuid(1000)
    assert next(line for line in Path('/proc/self/status').read_text().splitlines()
                if line.startswith('CapEff:')).split()[1] == '0000000000000000'
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM if protocol == 'udp' else socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        if source:
            sock.bind((source, 0))
        if device:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, device.encode() + b'\0')
        try:
            if protocol == 'udp':
                sock.sendto(b'vpn-fixture', (address, int(port)))
                result = sock.recvfrom(128)[0]
            else:
                sock.connect((address, int(port)))
                sock.sendall(b'vpn-fixture')
                result = sock.recv(128)
        except OSError:
            result = b''
    print(json.dumps(result.decode()))


def fixture():
    # These mounts and every network interface exist only in child namespaces.
    protocols = Path('/etc/protocols').read_bytes()
    run('mount', '-t', 'tmpfs', 'tmpfs', '/run')
    run('mount', '-t', 'tmpfs', 'tmpfs', '/etc')
    Path('/etc/protocols').write_bytes(protocols)
    Path('/etc/vpn').mkdir()
    run('ip', 'link', 'set', 'lo', 'up')
    with ExitStack() as cleanup, tempfile.TemporaryDirectory() as temporary:
        directory = Path(temporary)

        def child(*args):
            process = subprocess.Popen(['unshare', '--net', sys.executable, str(SCRIPT), *args],
                                       stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
            cleanup.callback(lambda: (process.terminate(), process.wait(timeout=5)))
            if process.stdout.readline().strip() != 'READY':
                raise RuntimeError('Private peer fixture failed to start')
            return ['nsenter', '-t', str(process.pid), '--net']

        provider = child('--provider')
        run('ip', 'link', 'add', 'eth0', 'type', 'veth', 'peer', 'name', 'uplink')
        run('ip', 'link', 'set', 'uplink', 'netns', provider[2])
        run('ip', 'address', 'add', '10.42.0.2/24', 'dev', 'eth0')
        run('ip', 'link', 'set', 'eth0', 'up')
        run(*provider, 'ip', 'address', 'add', '10.42.0.1/24', 'dev', 'uplink')
        run(*provider, 'ip', 'link', 'set', 'uplink', 'up')
        run('ip', 'route', 'add', 'default', 'via', '10.42.0.1')
        client_key, server_key = run('wg', 'genkey').strip(), run('wg', 'genkey').strip()
        client_public = run('wg', 'pubkey', input=client_key).strip()
        server_public = run('wg', 'pubkey', input=server_key).strip()
        server_config = directory / 'server.conf'
        server_config.write_text(f'[Interface]\nPrivateKey = {server_key}\nListenPort = 443\n'
                                 f'[Peer]\nPublicKey = {client_public}\nAllowedIPs = 10.64.0.2/32\n')
        run(*provider, 'ip', 'link', 'add', 'vpn-server', 'type', 'wireguard')
        run(*provider, 'wg', 'setconf', 'vpn-server', str(server_config))
        run(*provider, 'ip', 'address', 'add', '10.64.0.1/24', 'dev', 'vpn-server')
        run(*provider, 'ip', 'link', 'set', 'vpn-server', 'up')
        values = {'address': '10.64.0.2/32', 'endpoint-address': '198.51.100.1', 'endpoint-port': '443',
                  'peer-port': '10837', 'forwarded-port': '10837',
                  'wg.conf': f'[Interface]\nPrivateKey = {client_key}\n[Peer]\nPublicKey = {server_public}\n'
                             'Endpoint = 198.51.100.1:443\nAllowedIPs = 0.0.0.0/0\nPersistentKeepalive = 25\n'}
        for name, value in values.items():
            Path('/etc/vpn', name).write_text(value + '\n')
        # A capability-free root process cannot traverse a runner-owned private
        # home. Match Kubernetes's root-owned ConfigMap mount with identical bytes.
        initializer_script = directory / 'network.sh'
        initializer_script.write_bytes(NETWORK.read_bytes())

        def check(prefix, address, port=18080, *, protocol='tcp', expected='vpn-fixture', source='', device=''):
            result = json.loads(run(*prefix, sys.executable, str(SCRIPT), '--probe', address,
                                    str(port), protocol, source, device))
            if result != expected:
                raise AssertionError(f'{address}:{port}/{protocol} expected {expected!r}, got {result!r}')

        # Establish successful direct TCP/UDP and DNS baselines before filtering.
        for protocol in ('tcp', 'udp'):
            check([], '198.51.100.55', protocol=protocol)
            check([], '1.1.1.1', 53, protocol=protocol)
        initializer = ['setpriv', '--bounding-set=-all,+net_admin', '/bin/sh', str(initializer_script)]
        run(*initializer)
        run(*initializer)  # A failed init can retry after routes already changed.
        application = subprocess.Popen([sys.executable, str(SCRIPT), '--application'],
                                       stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        cleanup.callback(lambda: (application.terminate(), application.wait(timeout=5)))
        assert application.stdout.readline().strip() == 'READY'
        for protocol in ('tcp', 'udp'):
            check([], '198.51.100.55', protocol=protocol)
            check([], '1.1.1.1', 53, protocol=protocol)
            check([], '198.51.100.55', protocol=protocol, device='eth0', expected='')
            check(provider, '10.64.0.2', 10837, protocol=protocol,
                  source='198.51.100.55', expected='198.51.100.55')
        check(provider, '10.42.0.2', 8080)

        run(*provider, 'iptables', '-I', 'INPUT', '-p', 'udp', '--dport', '443', '-j', 'DROP')
        for protocol in ('tcp', 'udp'):
            check([], '198.51.100.55', protocol=protocol, expected='')
            check([], '198.51.100.55', protocol=protocol, device='eth0', expected='')
        check(provider, '10.42.0.2', 8080)
        run(*provider, 'iptables', '-D', 'INPUT', '-p', 'udp', '--dport', '443', '-j', 'DROP')
        check([], '198.51.100.55')
        run('ip', 'link', 'set', 'wg0', 'down')
        run('ip', 'route', 'replace', 'default', 'via', '10.42.0.1', 'dev', 'eth0')
        for protocol in ('tcp', 'udp'):
            check([], '198.51.100.55', protocol=protocol, expected='')
        print('Peer IPs, incoming TCP/UDP, tunneled DNS, retry, recovery and fail-closed egress passed.')


class VPNNetworkTests(unittest.TestCase):
    def test_initializer_preserves_peers_and_blocks_bypasses(self):
        namespace = [] if os.geteuid() == 0 else ['--user', '--map-auto', '--map-root-user']
        result = subprocess.run(['unshare', *namespace, '--mount', '--net',
                                 sys.executable, str(SCRIPT), '--fixture'], capture_output=True, text=True, timeout=100)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == '__main__':
    if sys.argv[1:] == ['--provider']:
        run('ip', 'link', 'set', 'lo', 'up')
        for address in ('198.51.100.1/32', '198.51.100.55/32', '1.1.1.1/32'):
            run('ip', 'address', 'add', address, 'dev', 'lo')
        echo('198.51.100.55', 18080)
        echo('1.1.1.1', 53)
        print('READY', flush=True)
        sys.stdin.read()
    elif sys.argv[1:] == ['--application']:
        os.setgroups([])
        os.setgid(1000)
        os.setuid(1000)
        echo('10.64.0.2', 10837, report_peer=True)
        echo('0.0.0.0', 8080)
        print('READY', flush=True)
        sys.stdin.read()
    elif sys.argv[1:2] == ['--probe']:
        probe(*sys.argv[2:])
    elif sys.argv[1:] == ['--fixture']:
        fixture()
    else:
        unittest.main()
