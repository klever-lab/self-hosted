"""Check the WebUI archive before it can become an active interface."""

import hashlib
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

SPEC = importlib.util.spec_from_file_location('install_webui', Path(__file__).resolve().parents[1] / 'apps/qbittorrent/install_webui.py')
webui = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(webui)


class WebUIInstallTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def install(self, members, *, checksum=None):
        archive = self.root / 'source.zip'
        with zipfile.ZipFile(archive, 'w') as output:
            for name, content in members.items():
                output.writestr(name, content)
        pin = {'version': 'fixture', 'url': 'https://example.test/webui.zip',
               'sha256': checksum or hashlib.sha256(archive.read_bytes()).hexdigest()}
        def download(args, **kwargs):
            Path(args[-1]).write_bytes(archive.read_bytes())
        with patch.object(webui.subprocess, 'run', side_effect=download) as fetch:
            webui.install(pin, self.root / 'installed')
            webui.install(pin, self.root / 'installed')
        self.assertEqual(fetch.call_count, 1)

    def test_verified_archive_is_published_once(self):
        self.install({'vuetorrent/public/index.html': '<html>fixture</html>'})
        self.assertEqual((self.root / 'installed/fixture/public/index.html').read_text(), '<html>fixture</html>')

    def test_bad_checksum_cannot_publish_an_interface(self):
        with self.assertRaisesRegex(ValueError, 'checksum'):
            self.install({'vuetorrent/public/index.html': 'fixture'}, checksum='0' * 64)
        self.assertFalse((self.root / 'installed/fixture').exists())

    def test_archive_cannot_write_outside_the_staging_directory(self):
        with self.assertRaisesRegex(ValueError, 'Unsafe'):
            self.install({'vuetorrent/public/index.html': 'fixture', '../outside': 'unsafe'})
        self.assertFalse((self.root / 'outside').exists())
