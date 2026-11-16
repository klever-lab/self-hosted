"""Exercise batch limits, EPUB identity, and interrupted upload recovery."""

import io
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import gutenberg
import requests


def epub(pg_id):
    data = io.BytesIO()
    with zipfile.ZipFile(data, 'w') as archive:
        archive.writestr('mimetype', 'application/epub+zip')
        archive.writestr('META-INF/container.xml', '''<container
            xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles>
            <rootfile full-path="content.opf"/></rootfiles></container>''')
        archive.writestr('content.opf', f'''<package xmlns="http://www.idpf.org/2007/opf"
            xmlns:dc="http://purl.org/dc/elements/1.1/"><metadata>
            <dc:identifier id="id">https://www.gutenberg.org/ebooks/{pg_id}</dc:identifier>
            </metadata></package>''')
    return data.getvalue()


class ImportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state = self.root / 'state'
        self.state.mkdir()
        self.books = [{'gutenberg_id': i, 'file': f'{i}/pg{i}-images-3.epub',
                       'title': f'Book {i}', 'author': 'Author', 'size_bytes': 1024}
                      for i in range(1, 201)]
        path = self.root / 'integrations/books/greatest-200.json'
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({'source': 'https://thegreatestbooks.org/', 'books': self.books}))
        self.library = {}
        self.posts = 0
        self.lose_receipt = False
        self.reject_upload = False

        def snapshot():
            return {'gutenberg': self.library.copy(), 'free_bytes': 100 * 2**30,
                    'book_ids': list(self.library.values())}

        def download(books, destination):
            for book in books:
                file = destination / book['file']
                file.parent.mkdir(parents=True, exist_ok=True)
                file.write_bytes(epub(book['gutenberg_id']))

        owner = self

        class Session:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def get(self, *args, **kwargs):
                return SimpleNamespace(text='<input name="csrf_token" value="token">',
                                       raise_for_status=lambda: None)

            def post(self, url, *, data, **kwargs):
                owner.posts += 1
                if owner.reject_upload:
                    raise requests.Timeout('The request failed before reaching the server')
                pg_id = int(Path(data.fields['btn-upload'][0]).stem.split('-')[0][2:])
                body = b''.join(iter(lambda: data.read(1024), b''))
                owner.assertIn(f'https://www.gutenberg.org/ebooks/{pg_id}'.encode(), body)
                book_id = len(owner.library) + 10
                owner.library[pg_id] = book_id
                if owner.lose_receipt:
                    raise requests.Timeout('The server committed but the response was lost')
                return SimpleNamespace(status_code=200, raise_for_status=lambda: None,
                                       json=lambda: {'location': f'/book/{book_id}'})

        for name, value in [('library_snapshot', snapshot), ('download', download),
                            ('login', lambda *args: Session()), ('time.sleep', lambda *args: None)]:
            patcher = patch('gutenberg.' + name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_import(self, **kwargs):
        gutenberg.import_books({'hostnames': {'calibre-web': 'books'}, 'dns_suffix': 'example.ts.net'},
            {'import-password': 'test-password'}, self.state, self.root, **kwargs)

    def test_fixed_batch_skips_existing_and_repeated_runs(self):
        self.library[1] = 7
        self.run_import(limit=3)
        self.assertEqual(set(self.library), {1, 2, 3})
        self.assertEqual(self.posts, 2)
        self.run_import(limit=3)
        self.assertEqual(self.posts, 2)
        receipt = json.loads((self.state / 'gutenberg-top-200/completion.json').read_text())
        self.assertEqual(receipt['selected'], 3)
        self.assertEqual(len(receipt['library_books']), 3)

    def test_lost_receipt_reconciles_without_uploading_again(self):
        self.lose_receipt = True
        with self.assertRaises(requests.Timeout):
            self.run_import(limit=1)
        with sqlite3.connect(self.state / 'gutenberg-top-200/imports.sqlite3') as ledger:
            self.assertEqual(ledger.execute('SELECT status FROM imports').fetchone(), ('pending',))
        self.run_import(limit=1)
        self.assertEqual(self.posts, 1)
        with sqlite3.connect(self.state / 'gutenberg-top-200/imports.sqlite3') as ledger:
            self.assertEqual(ledger.execute('SELECT status FROM imports').fetchone(), ('imported',))

    def test_uncertain_missing_upload_stops_for_review(self):
        self.reject_upload = True
        with self.assertRaises(requests.Timeout):
            self.run_import(limit=1)
        with self.assertRaisesRegex(RuntimeError, 'earlier upload to review'):
            self.run_import(limit=1)
        self.assertEqual(self.posts, 1)
        self.assertFalse(self.library)

    def test_limit_and_dry_run_cannot_expand_the_batch(self):
        with self.assertRaises(ValueError):
            self.run_import(limit=201)
        self.run_import(limit=2, dry_run=True)
        self.assertEqual(self.posts, 0)
        self.assertFalse((self.state / 'gutenberg-top-200/imports.sqlite3').exists())

    def test_download_must_match_the_selected_gutenberg_id(self):
        path = self.root / 'wrong.epub'
        path.write_bytes(epub(123))
        with self.assertRaisesRegex(ValueError, 'does not identify Gutenberg book 456'):
            gutenberg.validate_epub(path, 456)

    def test_manifest_rejects_unbounded_or_unexpected_downloads(self):
        path = self.root / 'integrations/books/greatest-200.json'
        manifest = json.loads(path.read_text())
        manifest['books'][0]['file'] = '../some-other-file.epub'
        path.write_text(json.dumps(manifest))
        with self.assertRaisesRegex(ValueError, 'Unexpected Gutenberg download path'):
            gutenberg.read_manifest(path)


if __name__ == '__main__':
    unittest.main()
