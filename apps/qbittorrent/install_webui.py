"""Install the checksum-pinned VueTorrent release through the VPN tunnel."""

import hashlib
import json
from pathlib import Path, PurePosixPath
import stat
import subprocess
import tempfile
import zipfile


def install(pin, root=Path('/config/webui')):
    destination = root / pin['version']
    marker = destination / '.archive-sha256'
    if marker.is_file() and marker.read_text().strip() == pin['sha256']:
        return
    root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=root) as temporary:
        stage = Path(temporary)
        archive = stage / 'vuetorrent.zip'
        subprocess.run(['curl', '--fail', '--silent', '--show-error', '--location',
                        '--retry', '2', '--connect-timeout', '15', '--max-time', '90',
                        pin['url'], '--output', str(archive)], check=True)
        if hashlib.sha256(archive.read_bytes()).hexdigest() != pin['sha256']:
            raise ValueError('VueTorrent archive checksum mismatch')
        with zipfile.ZipFile(archive) as contents:
            for item in contents.infolist():
                path = PurePosixPath(item.filename)
                if path.is_absolute() or '..' in path.parts or path.parts[0] != 'vuetorrent' or stat.S_ISLNK(item.external_attr >> 16):
                    raise ValueError('Unsafe VueTorrent archive member')
            contents.extractall(stage)
        extracted = stage / 'vuetorrent'
        if not (extracted / 'public/index.html').is_file():
            raise ValueError('VueTorrent public/index.html is missing')
        (extracted / '.archive-sha256').write_text(pin['sha256'] + '\n')
        extracted.rename(destination)


if __name__ == '__main__':
    install(json.loads(Path('/bootstrap/vuetorrent.json').read_text()))
    print('Pinned VueTorrent interface is ready.')
