"""Exercise duplicate uploads, cart recovery and verified snapshot restores."""

import base64
from collections import defaultdict
import json
from pathlib import Path
import shutil
import sqlite3
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import Mock, patch
from xml.etree import ElementTree

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'integrations/books'))
sys.path.insert(0, str(ROOT / 'scripts'))

from book_storage import BookStorage, sha256
import books_recovery
import import_books
import restore_books
import usenet_cart
from althub_api import cart_feed


class StorageFixture:
    def __init__(self):
        self.data = {}
        self.metadata = {}

    def put(self, key, file, digest, name=None):
        self.data.setdefault(key, file.read_bytes())
        self.metadata.setdefault(key, {'x-amz-meta-sha256': digest, 'x-amz-meta-filename': name,
                                       'content-length': str(file.stat().st_size)})

    def head(self, key):
        return self.metadata.get(key)

    def objects(self, prefix):
        return iter(sorted(key for key in self.data if key.startswith(prefix)))

    def download(self, key, path, digest):
        path.write_bytes(self.data[key])
        if sha256(path) != digest:
            raise RuntimeError('SHA-256 mismatch')


class ConditionalWrites(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.file = Path(directory.name) / 'book.epub'
        self.file.write_bytes(b'test book')
        self.digest = sha256(self.file)
        self.storage = BookStorage({'bucket': 'fixture-books', 'region': 'bhs',
                                    'access_key': 'fixture', 'secret_key': 'test-secret'})

    def test_conditional_write_handles_another_publisher_winning_the_race(self):
        metadata = {'x-amz-meta-sha256': self.digest, 'content-length': str(self.file.stat().st_size)}
        with patch.object(self.storage, 'head', side_effect=[None, metadata]), \
                patch.object(self.storage, 'request', return_value=(412, '')) as request:
            self.storage.put('inbox/' + self.digest, self.file, self.digest, name='My book.epub')
        self.assertIn('If-None-Match: *', request.call_args.kwargs['headers'])
        self.assertIn('x-amz-content-sha256: ' + self.digest, request.call_args.kwargs['headers'])
        self.assertIn('x-amz-meta-filename: My%20book.epub', request.call_args.kwargs['headers'])

    def test_unverified_existing_object_cannot_be_acknowledged(self):
        with patch.object(self.storage, 'head', return_value={'x-amz-meta-sha256': 'other'}), \
                patch.object(self.storage, 'request') as request:
            with self.assertRaisesRegex(RuntimeError, 'verify'):
                self.storage.put('inbox/' + self.digest, self.file, self.digest)
            request.assert_not_called()

    def test_listing_uses_continuation_token_and_keeps_the_prefix(self):
        responses = [
            (200, '<ListBucketResult><Contents><Key>inbox/a</Key></Contents><IsTruncated>true</IsTruncated>'
                  '<NextContinuationToken>next</NextContinuationToken></ListBucketResult>'),
            (200, '<ListBucketResult><Contents><Key>inbox/b</Key></Contents></ListBucketResult>'),
        ]
        calls = []
        def response(*args, params):
            calls.append(dict(params))
            return responses.pop(0)
        with patch.object(self.storage, 'request', side_effect=response):
            self.assertEqual(list(self.storage.objects('inbox/')), ['inbox/a', 'inbox/b'])
        self.assertEqual(calls[-1], {'list-type': '2', 'prefix': 'inbox/', 'continuation-token': 'next'})

    def test_curl_receives_credentials_over_stdin_and_cannot_follow_redirects(self):
        with patch('book_storage.subprocess.run', return_value=Mock(returncode=0, stdout='\n302')) as run:
            with self.assertRaisesRegex(RuntimeError, 'HTTP 302'):
                self.storage.request('GET')
        self.assertNotIn('test-secret', repr(run.call_args.args))
        self.assertNotIn('--location', run.call_args.args[0])
        self.assertIn('test-secret', run.call_args.kwargs['input'])


class CartTests(unittest.TestCase):
    identifier = 'a' * 32

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.release = self.root / 'complete/books/SAB'
        self.release.mkdir(parents=True)
        (self.release / 'first.epub').write_bytes(b'first book')
        (self.release / 'second.epub').write_bytes(b'second book')
        self.job = {'status': 'Completed', 'category': 'books', 'storage': str(self.release),
                    'nzo_id': 'SABnzbd_nzo_test', 'nzb_name': 'althub-' + self.identifier}
        self.storage = StorageFixture()
        self.alt = Mock()
        self.alt.cart_items.return_value = {self.identifier}
        self.alt.nzb.return_value = ElementTree.fromstring('<nzb><file/></nzb>')
        for change in (patch.object(import_books, 'DOWNLOADS', self.root),
                       patch.object(usenet_cart, 'AltHub', return_value=self.alt),
                       patch.object(usenet_cart, 'read_secret', return_value='fixture')):
            change.start()
            self.addCleanup(change.stop)

    def test_feed_disables_provider_side_removal_before_polling(self):
        feed = cart_feed('https://althub.co.za/rss?t=-2&del=1&dl=1&i=42&r=fixture')
        self.assertIn('del=0', feed)
        self.assertIn('dl=0', feed)
        self.assertNotIn('del=1', feed)
        with self.assertRaises(ValueError):
            cart_feed('https://outside.test/rss?t=-2&i=42&r=fixture')

    def test_remove_cart_entry_only_after_every_book_and_durable_receipt(self):
        def remove(action, identifier):
            receipt = json.loads(self.storage.data[usenet_cart.RELEASES + identifier + '.json'])
            for digest in receipt['sha256']:
                self.assertIn('inbox/' + digest, self.storage.data)
            self.assertEqual(action, 'remove')
        self.alt.cart.side_effect = remove
        with patch.object(usenet_cart, 'sab_jobs', side_effect=lambda mode: [self.job] if mode == 'history' else []):
            usenet_cart.poll(self.storage)
            usenet_cart.poll(self.storage)
        self.assertEqual(self.alt.cart.call_count, 2)
        self.alt.nzb.assert_not_called()
        self.assertEqual(len(list(self.storage.objects('inbox/'))), 2)

    def test_partial_upload_keeps_cart_and_does_not_publish_release_receipt(self):
        put = self.storage.put
        def interrupt(key, file, digest, name=None):
            if name == 'second.epub':
                raise RuntimeError('transient upload error')
            put(key, file, digest, name)
        self.storage.put = interrupt
        with patch.object(usenet_cart, 'sab_jobs', return_value=[self.job]):
            with self.assertRaisesRegex(RuntimeError, 'transient'):
                usenet_cart.poll(self.storage)
        self.assertFalse(list(self.storage.objects(usenet_cart.RELEASES)))
        self.alt.cart.assert_not_called()

    def test_cart_reconstructs_a_lost_download_and_respects_a_live_queue(self):
        shutil.rmtree(self.release)
        queue = []
        def jobs(mode):
            return [self.job] if mode == 'history' else queue
        def send(*args, **kwargs):
            queue.append({'filename': kwargs['nzbname']})
            return {'status': True, 'nzo_ids': ['next']}
        with patch.object(usenet_cart, 'sab_jobs', side_effect=jobs), \
                patch.object(usenet_cart, 'sab_request', side_effect=send) as submit:
            usenet_cart.poll(self.storage)
            usenet_cart.poll(self.storage)
        self.assertEqual(submit.call_count, 1)
        self.alt.cart.assert_not_called()

    def test_cutover_only_replaces_empty_grabs_with_unique_cart_matches(self):
        job = {'status': 'Grabbing', 'cat': 'books', 'mb': '0', 'filename': 'Book.NZB',
               'nzo_id': 'SABnzbd_nzo_old'}
        cart = {self.identifier: 'Book'}
        self.assertEqual(usenet_cart.recoverable_grabs([job], cart), ['SABnzbd_nzo_old'])
        for dangerous in ({**job, 'status': 'Downloading'}, {**job, 'mb': '1'}, {**job, 'filename': 'other'}):
            with self.assertRaisesRegex(RuntimeError, 'cannot be recreated'):
                usenet_cart.recoverable_grabs([dangerous], cart)
        with self.assertRaisesRegex(RuntimeError, 'cannot be recreated'):
            usenet_cart.recoverable_grabs([job], {**cart, 'b' * 32: 'Book'})


class RestoreTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        source = self.root / 'source'
        self.target = self.root / 'target'
        for directory in (source, self.target):
            for volume in ('config', 'library'):
                (directory / volume).mkdir(parents=True)
        with sqlite3.connect(source / 'library/metadata.db') as database:
            database.executescript("CREATE TABLE books(id INTEGER, uuid TEXT); INSERT INTO books VALUES(42,'original');")
        with sqlite3.connect(source / 'config/app.db') as database:
            database.executescript("CREATE TABLE user(id INTEGER); INSERT INTO user VALUES(4);")
        self.archive = self.root / 'snapshot.tar.gz'
        with tarfile.open(self.archive, 'w:gz') as archive:
            for volume in ('config', 'library'):
                archive.add(source / volume, arcname=volume)
        self.digest = sha256(self.archive)
        self.name = '20261117T223001Z-' + self.digest[:12]
        self.key = 'backups/' + self.name
        self.storage = StorageFixture()
        self.storage.put(self.key + '.tar.gz', self.archive, self.digest)
        manifest = self.root / 'manifest.json'
        manifest.write_text(json.dumps({'archive_sha256': self.digest, 'counts': [1, 1],
                                        'import_receipts': [['a' * 64, 'imported', 42]]}))
        self.storage.put(self.key + '.json', manifest, sha256(manifest))

    def test_restore_preserves_book_identity_and_seeds_the_shared_ledger(self):
        restore_books.restore(self.storage, self.name, self.target)
        with sqlite3.connect(self.target / 'library/metadata.db') as database:
            self.assertEqual(database.execute('SELECT * FROM books').fetchall(), [(42, 'original')])
        with sqlite3.connect(self.target / 'config/inbox/imports.sqlite3') as database:
            self.assertEqual(database.execute('SELECT status, book_id FROM imports').fetchall(), [('imported', 42)])
        with self.assertRaisesRegex(RuntimeError, 'empty'):
            restore_books.restore(self.storage, self.name, self.target)

    def test_archive_hash_is_checked_before_writing_any_claim(self):
        self.storage.data[self.key + '.tar.gz'] += b'invalid'
        with self.assertRaisesRegex(RuntimeError, 'SHA-256'):
            restore_books.restore(self.storage, self.name, self.target)
        self.assertEqual(list((self.target / 'config').iterdir()), [])
        self.assertEqual(list((self.target / 'library').iterdir()), [])


class UsenetStagingTests(unittest.TestCase):
    def test_staging_mounts_its_own_credentials_on_the_importer_path(self):
        existing_downloads = {'name': 'downloads', 'persistentVolumeClaim': {'claimName': 'downloads'}}
        live = {'spec': {'template': {'spec': {'volumes': [existing_downloads]}}}}
        template = {
            'securityContext': {},
            'containers': [{'name': 'books-inbox', 'volumeMounts': [
                {'name': 'downloads', 'mountPath': '/downloads'},
                {'name': 'books-credentials', 'mountPath': '/credentials'},
            ]}],
            'volumes': [
                {'name': 'downloads', 'emptyDir': {}},
                {'name': 'books-credentials', 'secret': {'secretName': 'book-importer'}},
            ],
        }
        items = [
            {'kind': 'Deployment', 'metadata': {'name': 'sabnzbd'},
             'spec': {'template': {'spec': template}}},
            {'kind': 'NetworkPolicy', 'metadata': {'name': 'sabnzbd'}, 'spec': {'egress': []}},
            {'kind': 'ConfigMap', 'metadata': {'name': 'books-tools-fixture', 'namespace': 'sabnzbd'}},
        ]
        applied = []
        credentials = {'althub-api-key': 'fixture'}
        with patch.object(books_recovery, 'kube_json', side_effect=[live, {'status': {'phase': 'Succeeded'}}]), \
                patch.object(books_recovery.manage, 'dry_run_pod'), \
                patch.object(books_recovery.manage, 'apply', side_effect=applied.extend), \
                patch.object(books_recovery.manage, 'kube', return_value=Mock(stdout=b'')):
            books_recovery.stage_usenet(items, {}, credentials)
        pod = next(item for item in applied if item['kind'] == 'Pod')
        name = pod['metadata']['name']
        volumes = {item['name']: item for item in pod['spec']['volumes']}
        self.assertEqual(volumes['books-credentials']['secret']['secretName'], name)
        self.assertEqual(volumes['downloads']['persistentVolumeClaim'],
                         {'claimName': 'downloads', 'readOnly': True})
        secret = next(item for item in applied if item['kind'] == 'Secret' and item['metadata']['name'] == name)
        self.assertEqual({key: base64.b64decode(value).decode() for key, value in secret['data'].items()}, credentials)


class SeedPauseTests(unittest.TestCase):
    def test_failed_export_resumes_only_prior_seeds_and_restores_force_start(self):
        session = Mock()
        session.__enter__ = Mock(return_value=session)
        session.__exit__ = Mock(return_value=False)
        calls = defaultdict(list)
        states = {'a': 'uploading', 'b': 'forcedUP', 'c': 'stoppedUP', 'd': 'downloading'}
        def send(method, url, data, **kwargs):
            endpoint = url.split('/api/v2/')[-1]
            calls[endpoint].append(data)
            if endpoint == 'torrents/stop':
                states.update(a='stoppedUP', b='stoppedUP')
            if endpoint == 'torrents/start':
                states.update(a='uploading')
            if endpoint == 'torrents/setForceStart':
                states.update(b='forcedUP')
            return Mock(status_code=200, json=lambda: [{'hash': key, 'state': value} for key, value in states.items()])
        session.request.side_effect = send
        cfg = {'hostnames': {'qbittorrent': 'qbit'}, 'dns_suffix': 'test.ts.net'}
        credential = {'qbittorrent': {'username': 'operator', 'password': 'fixture'}}
        with tempfile.TemporaryDirectory() as temporary, \
                patch.object(books_recovery.requests, 'Session', return_value=session), \
                patch.object(books_recovery.manage, 'load', return_value=credential):
            with self.assertRaisesRegex(RuntimeError, 'export failed'):
                with books_recovery.pause_seeds(cfg, Path(temporary)):
                    raise RuntimeError('export failed')
        self.assertEqual(calls['torrents/stop'], [{'hashes': 'a|b'}])
        self.assertEqual(calls['torrents/start'], [{'hashes': 'a'}])
        self.assertEqual(calls['torrents/setForceStart'], [{'hashes': 'b', 'value': 'true'}])
        self.assertEqual(states, {'a': 'uploading', 'b': 'forcedUP', 'c': 'stoppedUP', 'd': 'downloading'})


if __name__ == '__main__':
    unittest.main()
