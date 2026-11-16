"""Set required qBittorrent defaults without replacing torrent/session state."""

import base64
import configparser
import hashlib
import json
import os
from pathlib import Path

path = Path('/config/qBittorrent/config/qBittorrent.conf')
fresh = not path.exists()
config = configparser.ConfigParser(interpolation=None)
config.optionxform = str
if not fresh:
    config.read(path)
for section in ('BitTorrent', 'Network', 'WebUI', 'Preferences', 'Meta'):
    if not config.has_section(section):
        config.add_section(section)
config['Meta']['MigrationVersion'] = '12'

network = config['Network']
for key in list(network):
    if key.startswith('Proxy\\'):
        del network[key]
network.update({'Proxy\\Type': 'None', 'PortForwardingEnabled': 'false'})
config['BitTorrent'].update({
    'Session\\DefaultSavePath': '/downloads/complete',
    'Session\\DiskCacheSize': '2048',
    'Session\\TempPath': '/downloads/incomplete',
    'Session\\TempPathEnabled': 'true',
    'Session\\DHTEnabled': 'true',
    'Session\\LSDEnabled': 'false',
    'Session\\PeXEnabled': 'true',
    'Session\\BTProtocol': 'Both',
    'Session\\ProxyPeerConnections': 'false',
    'Session\\Interface': 'wg0',
    'Session\\InterfaceName': 'wg0',
    'Session\\InterfaceAddress': '',
    'Session\\Port': Path('/etc/vpn/peer-port').read_text().strip(),
    'Session\\MultiConnectionsPerIp': 'false',
})
config['WebUI'].update({
    'Address': '*', 'Port': '8080',
    'LocalHostAuth': 'true', 'AuthSubnetWhitelistEnabled': 'false',
    'HostHeaderValidation': 'true', 'CSRFProtection': 'true',
    'ServerDomains': '"' + Path('/credentials/domain').read_text().strip() + ';qbittorrent;localhost"',
    'AlternativeUIEnabled': 'true',
    'RootFolder': '/config/webui/' + json.loads(Path('/bootstrap/vuetorrent.json').read_text())['version'],
})
config['Preferences']['Connection\\UPnP'] = 'false'
if fresh:
    salt = os.urandom(16)
    password = Path('/credentials/password').read_text().strip().encode()
    digest = hashlib.pbkdf2_hmac('sha512', password, salt, 100000)
    encoded = base64.b64encode(salt).decode() + ':' + base64.b64encode(digest).decode()
    config['WebUI']['Username'] = Path('/credentials/username').read_text().strip()
    config['WebUI']['Password_PBKDF2'] = '"@ByteArray(' + encoded + ')"'

path.parent.mkdir(parents=True, exist_ok=True)
with path.open('w') as output:
    config.write(output, space_around_delimiters=False)
path.chmod(0o600)
for directory in ('complete', 'incomplete', 'complete/books'):
    Path('/downloads', directory).mkdir(parents=True, exist_ok=True)
categories = path.parent / 'categories.json'
if not categories.exists():
    categories.write_text(json.dumps({'books': {'save_path': '/downloads/complete/books'}}))
print('qBittorrent configured with authenticated WebUI and VPN-only traffic.')
