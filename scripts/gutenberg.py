"""Import the fixed Gutenberg reading list through Calibre-Web's upload endpoint."""

import fcntl
import hashlib
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from urllib.parse import urlparse
from xml.etree import ElementTree
import zipfile

import requests
from requests_toolbelt.multipart.encoder import MultipartEncoder


MAX_BOOKS = 200
MAX_UPLOAD = 200 * 1024 * 1024
MIRROR = 'rsync://rsync.ibiblio.org/gutenberg-epub/'
MERGER_URL = ('https://raw.githubusercontent.com/JimmXinu/EpubMerge/'
              '8cc76632722a94abc2ad08ff0f5cbda83ffb96eb/epubmerge/epubmerge.py')
MERGER_SHA256 = 'b4b27cda8a0e03f38bbbb59aa5e63409f18290ab75bd524f082251d6e9887a6e'


def source_books(books):
    return [part for book in books for part in book.get('parts', [book])]


def gutenberg_id(value):
    match = re.fullmatch(r'https?://(?:www\.)?gutenberg\.org/(?:ebooks/)?(\d+)/?', value.strip())
    return int(match[1]) if match else None


def read_manifest(path):
    manifest = json.loads(path.read_text())
    books = manifest['books']
    if len(books) != MAX_BOOKS or len({book['gutenberg_id'] for book in books}) != MAX_BOOKS:
        raise ValueError('The reading list must contain exactly 200 distinct Gutenberg books')
    for book in source_books(books):
        book_id = book['gutenberg_id']
        if not re.fullmatch(rf'{book_id}/pg{book_id}(?:-images(?:-3)?)?\.epub', book['file']):
            raise ValueError('Unexpected Gutenberg download path')
        if not 0 < book['size_bytes'] <= MAX_UPLOAD:
            raise ValueError('A reading-list entry exceeds the upload limit')
    return manifest


def library_snapshot():
    # Calibre-Web remains the only writer. This only inspects identifiers and space.
    code = '''import json, os, sqlite3
c = sqlite3.connect('file:/books/metadata.db?mode=ro', uri=True)
s = os.statvfs('/books')
print(json.dumps({'identifiers': c.execute('SELECT book, type, val FROM identifiers').fetchall(),
                  'book_ids': [r[0] for r in c.execute('SELECT id FROM books')],
                  'free_bytes': s.f_bavail * s.f_frsize}))'''
    result = subprocess.run(['kubectl', '-n', 'calibre-web', 'exec', 'deploy/calibre-web',
        '-c', 'calibre-web', '--', 'python3', '-c', code], check=True, capture_output=True)
    snapshot = json.loads(result.stdout)
    snapshot['gutenberg'] = {}
    for book_id, kind, value in snapshot['identifiers']:
        pg_id = gutenberg_id(value)
        if pg_id is None and kind.lower() == 'gutenberg' and value.isdigit():
            pg_id = int(value)
        if pg_id is not None:
            snapshot['gutenberg'].setdefault(pg_id, book_id)
    return snapshot


class CSRFParser(HTMLParser):
    token = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'input' and attrs.get('name') == 'csrf_token':
            self.token = attrs.get('value')


def csrf(response):
    response.raise_for_status()
    parser = CSRFParser()
    parser.feed(response.text)
    if not parser.token:
        raise RuntimeError('Calibre-Web did not provide a CSRF token')
    return parser.token


def login(url, password):
    session = requests.Session()
    token = csrf(session.get(url + '/login', timeout=30))
    response = session.post(url + '/login', data={
        'username': 'book-importer', 'password': password, 'csrf_token': token,
    }, timeout=30)
    response.raise_for_status()
    if urlparse(response.url).path.rstrip('/') == '/login':
        session.close()
        raise RuntimeError('Calibre-Web importer authentication failed')
    return session


def validate_epub(path, expected_id):
    if not 0 < path.stat().st_size <= MAX_UPLOAD:
        raise ValueError('An EPUB exceeds the upload limit or is empty')
    with zipfile.ZipFile(path) as archive:
        if archive.read('mimetype') != b'application/epub+zip':
            raise ValueError('The download is not an EPUB')
        container = ElementTree.fromstring(archive.read('META-INF/container.xml'))
        rootfile = container.find('.//{urn:oasis:names:tc:opendocument:xmlns:container}rootfile')
        if rootfile is None:
            raise ValueError('The EPUB has no package document')
        package = ElementTree.fromstring(archive.read(rootfile.attrib['full-path']))
        identifiers = package.findall('.//{http://purl.org/dc/elements/1.1/}identifier')
        if expected_id not in {gutenberg_id(item.text or '') for item in identifiers}:
            raise ValueError(f'The EPUB does not identify Gutenberg book {expected_id}')
        bad_file = archive.testzip()
        if bad_file:
            raise ValueError('The EPUB contains a corrupt ZIP member')
    with path.open('rb') as source:
        return hashlib.file_digest(source, 'sha256').hexdigest()


def download(books, destination):
    if not books:
        return
    if not shutil.which('rsync'):
        raise RuntimeError('Install rsync on the operator computer before importing')
    destination.mkdir(mode=0o700, parents=True, exist_ok=True)
    print(f'Fetching {len(books)} selected EPUBs from Gutenberg\'s rsync mirror.', flush=True)
    # Only these explicit paths can be fetched; no recursive collection crawl.
    subprocess.run(['rsync', '--recursive', '--times', '--partial-dir=.rsync-partial',
        '--contimeout=20', '--timeout=120', '--max-size=200m',
        '--chmod=Du=rwx,Dgo=,Fu=rw,Fgo=', '--files-from=-', MIRROR, str(destination) + '/'],
        input=''.join(book['file'] + '\n' for book in books).encode(), check=True)


def prepare_epub(book, destination):
    sources = book.get('parts', [book])
    paths = [destination / 'downloads' / part['file'] for part in sources]
    for part, path in zip(sources, paths):
        validate_epub(path, part['gutenberg_id'])
    if len(paths) == 1:
        return paths[0]
    # EpubMerge preserves each volume's content and navigation without conversion.
    tool = destination / 'epubmerge-engine.py'
    if not tool.exists():
        response = requests.get(MERGER_URL, timeout=30)
        response.raise_for_status()
        if hashlib.sha256(response.content).hexdigest() != MERGER_SHA256:
            raise RuntimeError('The pinned EpubMerge download failed verification')
        with open(tool, 'wb', opener=lambda p, flags: os.open(p, flags, 0o600)) as output:
            output.write(response.content)
    if hashlib.sha256(tool.read_bytes()).hexdigest() != MERGER_SHA256:
        raise RuntimeError('The cached EpubMerge engine failed verification')
    merged = destination / f'combined-{book["gutenberg_id"]}.epub'
    subprocess.run([sys.executable, str(tool), '--output', str(merged),
        '--title', book['title'], '--author', book['author'],
        '--source', f'https://www.gutenberg.org/ebooks/{book["gutenberg_id"]}',
        *map(str, paths)], check=True)
    merged.chmod(0o600)
    validate_epub(merged, book['gutenberg_id'])
    return merged


def import_books(cfg, access, state, root, *, limit=MAX_BOOKS, dry_run=False):
    if not 1 <= limit <= MAX_BOOKS:
        raise ValueError('Choose between 1 and 200 books')
    path = root / 'integrations/books/greatest-200.json'
    manifest = read_manifest(path)
    books = manifest['books'][:limit]
    destination = state / 'gutenberg-top-200'
    destination.mkdir(mode=0o700, parents=True, exist_ok=True)
    with open(destination / 'import.lock', 'w', opener=lambda p, flags: os.open(p, flags, 0o600)) as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        snapshot = library_snapshot()
        present = snapshot['gutenberg']
        missing = [book for book in books if book['gutenberg_id'] not in present]
        size = sum(book['size_bytes'] for book in source_books(missing))
        print(f'Selected {len(books)} books; {len(books) - len(missing)} already present; '
              f'{len(missing)} to import ({size / 2**20:.1f} MiB of EPUBs).', flush=True)
        if dry_run:
            return
        if snapshot['free_bytes'] < size * 3 + 5 * 2**30:
            raise RuntimeError('The library needs more free space before importing this batch')
        if shutil.disk_usage(destination).free < size * 2 + 2**30:
            raise RuntimeError('The operator computer needs more free space for this batch')
        ledger_path = destination / 'imports.sqlite3'
        ledger = sqlite3.connect(ledger_path)
        ledger_path.chmod(0o600)
        ledger.execute('CREATE TABLE IF NOT EXISTS imports '
                       '(gutenberg_id INTEGER PRIMARY KEY, status TEXT NOT NULL, '
                       'book_id INTEGER, sha256 TEXT)')
        for book in books:
            pg_id = book['gutenberg_id']
            if pg_id in present:
                if book.get('parts') and not ledger.execute(
                        'SELECT 1 FROM imports WHERE gutenberg_id=?', (pg_id,)).fetchone():
                    ledger.close()
                    raise RuntimeError(f'Gutenberg {pg_id} is already present; check that all volumes '
                                       'are included before recording this work as imported')
                ledger.execute('INSERT INTO imports VALUES (?, ?, ?, NULL) '
                               'ON CONFLICT(gutenberg_id) DO UPDATE SET status=excluded.status, '
                               'book_id=excluded.book_id', (pg_id, 'imported', present[pg_id]))
            elif ledger.execute('SELECT 1 FROM imports WHERE gutenberg_id=?', (pg_id,)).fetchone():
                ledger.close()
                raise RuntimeError(f'Gutenberg {pg_id} has an earlier upload to review in {ledger_path}; '
                                   'see docs/media-imports.md')
        ledger.commit()
        url = f'https://{cfg["hostnames"]["calibre-web"]}.{cfg["dns_suffix"]}'
        imported = 0
        try:
            download(source_books(missing), destination / 'downloads')
            with login(url, access['import-password']) as session:
                for book in missing:
                    pg_id = book['gutenberg_id']
                    file = prepare_epub(book, destination)
                    digest = validate_epub(file, pg_id)
                    token = csrf(session.get(url + '/', timeout=30))
                    # A receipt can be lost after the server commits. Reconcile by Gutenberg ID
                    # on the next run instead of automatically repeating an uncertain POST.
                    ledger.execute('INSERT INTO imports VALUES (?, ?, NULL, ?)', (pg_id, 'pending', digest))
                    ledger.commit()
                    with file.open('rb') as source:
                        body = MultipartEncoder(fields={'csrf_token': token,
                            'btn-upload': (file.name, source, 'application/epub+zip')})
                        response = session.post(url + '/upload', data=body,
                            headers={'Content-Type': body.content_type},
                            timeout=(30, 300), allow_redirects=False)
                    response.raise_for_status()
                    if response.status_code != 200:
                        raise RuntimeError('Calibre-Web did not confirm the upload')
                    match = re.fullmatch(r'/(?:admin/)?book/(\d+)', response.json().get('location', ''))
                    if not match:
                        raise RuntimeError('Calibre-Web did not provide a book ID for the upload')
                    book_id = int(match[1])
                    ledger.execute('UPDATE imports SET status=?, book_id=? WHERE gutenberg_id=?',
                                   ('imported', book_id, pg_id))
                    ledger.commit()
                    imported += 1
                    print(f'{imported}/{len(missing)}: {book["title"]} (Gutenberg {pg_id}, '
                          f'library {book_id})', flush=True)
                    time.sleep(0.25)
            verified = library_snapshot()
            absent = [book['gutenberg_id'] for book in books
                      if book['gutenberg_id'] not in verified['gutenberg']]
            if absent:
                raise RuntimeError(f'The library is missing expected Gutenberg IDs: {absent}')
            receipt = {'source': manifest['source'], 'verified_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
                       'manifest_sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                       'selected': len(books), 'newly_imported': imported,
                       'library_books': {str(book['gutenberg_id']): verified['gutenberg'][book['gutenberg_id']]
                                         for book in books}}
            with open(destination / 'completion.json', 'w',
                      opener=lambda p, flags: os.open(p, flags, 0o600)) as output:
                output.write(json.dumps(receipt, indent=2) + '\n')
            print(f'Verified {len(books)} selected books in the library; added {imported}.', flush=True)
        finally:
            ledger.close()
