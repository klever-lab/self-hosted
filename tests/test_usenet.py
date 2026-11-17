"""Protect retained downloader settings, completed-file boundaries and import receipts."""

from copy import deepcopy
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


bootstrap = module('sab_bootstrap', 'apps/sabnzbd/bootstrap.py')
importer = module('book_importer', 'integrations/books/import_books.py')
manage = module('usenet_manage', 'scripts/manage.py')


class DownloaderSettingsTests(unittest.TestCase):
    def setUp(self):
        self.credentials = {'username': 'operator', 'password': 'private-password',
                            'api-key': 'private-api', 'nzb-key': 'private-nzb', 'domain': 'usenet.example'}

    def test_redeployment_retains_provider_logins_feeds_and_custom_categories(self):
        config = {'servers': {'news.frugalusenet.com': {'username': 'provider-user',
                  'password': 'provider-password', 'enable': 1}},
                  'rss': {'cart': {'uri': ['https://example.test/private-feed']}},
                  'categories': {'custom': {'dir': 'custom'}}, 'misc': {'cache_limit': '128M'}}
        saved = deepcopy(config)
        bootstrap.configure(config, self.credentials)
        bootstrap.configure(config, self.credentials)
        for section in ('rss',):
            self.assertEqual(config[section], saved[section])
        self.assertEqual(config['servers']['news.frugalusenet.com'], saved['servers']['news.frugalusenet.com'])
        self.assertEqual(config['categories']['custom'], saved['categories']['custom'])
        self.assertEqual(config['misc']['cache_limit'], '128M')
        self.assertEqual(config['misc']['api_key'], 'private-api')
        self.assertEqual(config['misc']['complete_dir'], '/downloads/complete')
        self.assertEqual(config['categories']['books']['dir'], 'books')

    def test_new_servers_require_private_credentials_and_use_verified_tls(self):
        config = {}
        bootstrap.configure(config, self.credentials)
        for server in config['servers'].values():
            self.assertEqual((server['enable'], server['ssl'], server['ssl_verify'], server['port']), (0, 1, 3, 563))
        self.credentials.update({'frugal-username': 'provider-user', 'frugal-password': 'provider-password'})
        bootstrap.configure(config, self.credentials)
        for server in config['servers'].values():
            self.assertEqual((server['enable'], server['username'], server['password']),
                             (1, 'provider-user', 'provider-password'))
        self.assertLess(config['servers']['news.frugalusenet.com']['priority'],
                        config['servers']['bonus.frugalusenet.com']['priority'])


class UsenetFilesTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.downloads = self.root / 'downloads'
        self.books = self.downloads / 'complete/books'
        self.books.mkdir(parents=True)
        for mocked in (patch.object(importer, 'DOWNLOADS', self.downloads),
                       patch.object(importer, 'read_secret', return_value='private-api-key'),
                       patch.dict(os.environ, {'SABNZBD_URL': 'http://sabnzbd:8080'})):
            mocked.start()
            self.addCleanup(mocked.stop)

    def job(self, name, *, category='books', status='Completed'):
        directory = self.books / name
        directory.mkdir()
        (directory / 'book.epub').write_bytes(b'ebook-content')
        (directory / 'notes.nfo').write_text('not a book')
        return {'category': category, 'status': status, 'storage': str(directory)}

    def response(self, slots, total=None):
        response = Mock()
        response.json.return_value = {'history': {'slots': slots, 'noofslots': len(slots) if total is None else total}}
        return response

    def test_only_successful_book_jobs_are_read_and_originals_are_unchanged(self):
        slots = [self.job('complete'), self.job('repairing', status='Repairing'),
                 self.job('failed', status='Failed'), self.job('other', category='other')]
        expected = self.books / 'complete/book.epub'
        with patch.object(importer.requests, 'post', return_value=self.response(slots)) as post:
            self.assertEqual(list(importer.usenet_files()), [expected])
        self.assertEqual(expected.read_bytes(), b'ebook-content')
        args, kwargs = post.call_args
        self.assertNotIn('private-api-key', args[0])
        self.assertEqual(kwargs['data']['apikey'], 'private-api-key')

    def test_history_pagination_does_not_drop_older_books(self):
        responses = [self.response([self.job('first')], 2), self.response([self.job('second')], 2)]
        with patch.object(importer.requests, 'post', side_effect=responses) as post:
            self.assertEqual(len(list(importer.usenet_files())), 2)
        self.assertEqual([call.kwargs['data']['start'] for call in post.call_args_list], [0, 1])

    def test_successful_jobs_cannot_read_outside_books_or_follow_escaping_symlinks(self):
        outside = self.root / 'private.epub'
        outside.write_bytes(b'private')
        directory = self.books / 'symlink'
        directory.mkdir()
        (directory / 'escape.epub').symlink_to(outside)
        for storage in (str(outside), str(directory), str(self.books)):
            with self.subTest(storage=storage):
                job = {'category': 'books', 'status': 'Completed', 'storage': storage}
                with patch.object(importer.requests, 'post', return_value=self.response([job])):
                    with self.assertRaisesRegex(RuntimeError, 'outside'):
                        list(importer.usenet_files())

    def test_missing_files_and_api_errors_cannot_silently_succeed(self):
        jobs = [{'category': 'books', 'status': 'Completed', 'storage': ''},
                {'category': 'books', 'status': 'Completed', 'storage': str(self.books / 'missing')}]
        for job in jobs:
            with self.subTest(job=job), patch.object(importer.requests, 'post', return_value=self.response([job])):
                with self.assertRaises(RuntimeError):
                    list(importer.usenet_files())
        response = Mock()
        response.json.return_value = {'error': 'API key incorrect'}
        with patch.object(importer.requests, 'post', return_value=response):
            with self.assertRaisesRegex(RuntimeError, 'history'):
                list(importer.usenet_files())


class ImportReceiptTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.ledger = self.root / 'imports.sqlite3'
        self.files = [self.root / 'first.epub', self.root / 'copy.epub']
        for path in self.files:
            path.write_bytes(b'same-ebook')
        response = Mock(text='<input name="csrf_token" value="csrf">', url='http://calibre/')
        response.json.return_value = {'location': '/book/42'}
        self.session = Mock()
        self.session.get.return_value = response
        self.session.post.return_value = response
        for mocked in (patch.object(importer, 'LEDGER', self.ledger),
                       patch.object(importer, 'read_secret', return_value='private-password'),
                       patch.object(importer, 'usenet_files', side_effect=lambda: iter(self.files)),
                       patch.object(importer.requests, 'Session', return_value=self.session),
                       patch.dict(os.environ, {'SABNZBD_URL': 'http://sabnzbd', 'CALIBRE_URL': 'http://calibre'})):
            mocked.start()
            self.addCleanup(mocked.stop)

    def uploads(self):
        return [call for call in self.session.post.call_args_list if call.args[0].endswith('/upload')]

    def test_identical_downloads_and_later_runs_upload_only_once(self):
        importer.main()
        importer.main()
        self.assertEqual(len(self.uploads()), 1)
        with sqlite3.connect(self.ledger) as ledger:
            self.assertEqual(ledger.execute('SELECT status, book_id FROM imports').fetchall(), [('imported', 42)])
        for path in self.files:
            self.assertEqual(path.read_bytes(), b'same-ebook')

    def test_interrupted_upload_stays_pending_and_cannot_upload_again(self):
        def post(url, **kwargs):
            if url.endswith('/upload'):
                raise importer.requests.ConnectionError('interrupted response')
            return Mock(url='http://calibre/')
        self.session.post.side_effect = post
        with self.assertRaises(importer.requests.ConnectionError):
            importer.main()
        with sqlite3.connect(self.ledger) as ledger:
            self.assertEqual(ledger.execute('SELECT status FROM imports').fetchall(), [('pending',)])
        with self.assertRaisesRegex(RuntimeError, 'interrupted upload'):
            importer.main()
        self.assertEqual(len(self.uploads()), 1)


class BackupRecoveryTests(unittest.TestCase):
    def test_failed_export_restores_writers_and_each_original_import_schedule(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            for name in ('config.json', 'access.json', 'wireguard.conf'):
                (state / name).write_text('fixture')
            calls = []
            original = {'qbittorrent': False, 'sabnzbd': True}

            def kube(*args, **kwargs):
                calls.append(args)
                payload = {}
                if args[:2] == ('get', 'deployments'):
                    payload = {'items': [{'metadata': {'name': app, 'namespace': app}} for app in manage.APPS]}
                elif args[2:4] == ('get', 'cronjob'):
                    if args[1] not in original:
                        return SimpleNamespace(stdout=b'', stderr=b'', returncode=0)
                    payload = {'spec': {'suspend': original[args[1]]}}
                elif args[2:4] == ('get', 'jobs'):
                    payload = {'items': []}
                elif args[2:4] == ('get', 'deployment'):
                    payload = {'spec': {'replicas': 1}}
                return SimpleNamespace(stdout=json.dumps(payload).encode(), stderr=b'', returncode=0)

            with patch.object(manage, 'STATE', state), patch.object(manage, 'kube', side_effect=kube), \
                    patch.object(manage, 'apply', side_effect=RuntimeError('export could not start')):
                with self.assertRaisesRegex(RuntimeError, 'export could not start'):
                    manage.backup({'wireguard_profile': str(state / 'wireguard.conf')})
            for app in manage.APPS:
                scales = [call[-1] for call in calls if call[:4] == ('-n', app, 'scale', 'deployment/' + app)]
                self.assertEqual(scales, ['--replicas=0', '--replicas=1'])
            for app, initial in original.items():
                suspends = [json.loads(call[-1])['spec']['suspend'] for call in calls
                            if call[:4] == ('-n', app, 'patch', 'cronjob')]
                self.assertEqual(suspends, [True, initial])


if __name__ == '__main__':
    unittest.main()
