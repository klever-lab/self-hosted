"""Send completed downloads to OVH and import verified books into Calibre-Web."""

import argparse
from contextlib import closing
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import time
from urllib.parse import unquote

import requests

from book_storage import BookStorage, sha256

DOWNLOADS = Path('/downloads')
LEDGER = Path('/state/imports.sqlite3')
BOOK_FORMATS = {'.epub', '.kepub', '.pdf', '.mobi', '.azw3', '.fb2'}
MAX_BOOK_BYTES = 200 * 1024 * 1024
BUCKET_CONFIG = Path('/storage/credentials.json')
INBOX = 'inbox/'


class CSRFParser(HTMLParser):
    token = None

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == 'input' and values.get('name') == 'csrf_token':
            self.token = values.get('value')


def csrf(response):
    response.raise_for_status()
    parser = CSRFParser()
    parser.feed(response.text)
    if not parser.token:
        raise RuntimeError('Calibre-Web did not provide a CSRF token')
    return parser.token


def read_secret(name):
    return Path('/credentials', name).read_text().strip()


def book_file(path, root):
    file = path.resolve()
    if not file.is_relative_to(root.resolve()):
        raise RuntimeError('A completed file resolves outside the download directory')
    if file.suffix.lower() not in BOOK_FORMATS:
        return None
    if not file.is_file():
        raise RuntimeError('A completed book file is missing')
    if not 0 < file.stat().st_size <= MAX_BOOK_BYTES:
        raise RuntimeError('A book is empty or exceeds the 200 MiB upload limit')
    return file


def torrent_files():
    qbt_url = os.environ['QBITTORRENT_URL'].rstrip('/')
    qbt = requests.Session()
    response = qbt.post(qbt_url + '/api/v2/auth/login', data={
        'username': read_secret('qbt-username'), 'password': read_secret('qbt-password')
    }, headers={'Referer': qbt_url}, timeout=30)
    response.raise_for_status()
    if response.status_code != 204 and response.text.strip() != 'Ok.':
        raise RuntimeError('qBittorrent authentication failed')
    response = qbt.get(qbt_url + '/api/v2/torrents/info', params={'category': 'books'}, timeout=30)
    response.raise_for_status()
    torrents = [t for t in response.json() if t['progress'] == 1 and t.get('amount_left', 0) == 0]
    for torrent in torrents:
        response = qbt.get(qbt_url + '/api/v2/torrents/files', params={'hash': torrent['hash']}, timeout=30)
        response.raise_for_status()
        for item in response.json():
            if item['progress'] == 1:
                file = book_file(Path(torrent['save_path']) / item['name'], DOWNLOADS)
                if file:
                    yield file


def sab_request(mode, *, files=None, **params):
    url = os.environ['SABNZBD_URL'].rstrip('/')
    # Send credentials in the POST body.
    response = requests.post(url + '/api', data={
        'mode': mode, 'output': 'json', 'apikey': read_secret('sab-api-key'), **params
    }, files=files, timeout=(10, 30))
    response.raise_for_status()
    result = response.json()
    if result.get('status') is False:
        raise RuntimeError(f'SABnzbd rejected its {mode} request')
    return result


def sab_jobs(mode, category='books'):
    start = 0
    while True:
        params = {'cat': category} if category else {}
        payload = sab_request(mode, start=start, limit=100, **params)
        if mode not in payload:
            raise RuntimeError(f'SABnzbd did not return its {mode}')
        queue = payload[mode]
        slots = queue['slots']
        yield from slots
        start += len(slots)
        if not slots or start >= int(queue['noofslots']):
            break


def usenet_directory(job):
    root = (DOWNLOADS / 'complete/books').resolve()
    if not job.get('storage'):
        raise RuntimeError('A completed Usenet job has no storage path')
    directory = Path(job['storage']).resolve()
    if not directory.is_relative_to(root) or directory == root:
        raise RuntimeError('A Usenet job resolves outside its book directory')
    return directory


def usenet_files(job):
    directory = usenet_directory(job)
    if not directory.exists():
        raise RuntimeError('A completed Usenet download is missing')
    paths = directory.rglob('*') if directory.is_dir() else (directory,)
    for path in paths:
        file = book_file(path, DOWNLOADS / 'complete/books')
        if file:
            yield file


def put_book(storage, file):
    digest = sha256(file)
    storage.put(INBOX + digest, file, digest, name=file.name)
    return digest


def open_ledger(path):
    ledger = sqlite3.connect(path)
    ledger.execute('CREATE TABLE IF NOT EXISTS imports '
                   '(sha256 TEXT PRIMARY KEY, status TEXT NOT NULL, book_id INTEGER)')
    return ledger


def connect_calibre(calibre):
    url = os.environ['CALIBRE_URL'].rstrip('/')
    token = csrf(calibre.get(url + '/login', timeout=30))
    response = calibre.post(url + '/login', data={
        'username': 'book-importer', 'password': read_secret('calibre-password'), 'csrf_token': token
    }, timeout=30)
    response.raise_for_status()
    if '/login' in response.url:
        raise RuntimeError('Calibre-Web importer authentication failed')
    return url


def import_book(calibre, url, ledger, file, digest):
    token = csrf(calibre.get(url + '/', timeout=30))
    # Commit intent first. An interrupted response must not silently create duplicates.
    with ledger:
        reserved = ledger.execute('INSERT OR IGNORE INTO imports VALUES (?, ?, NULL)', (digest, 'pending'))
    if reserved.rowcount == 0:
        status, = ledger.execute('SELECT status FROM imports WHERE sha256=?', (digest,)).fetchone()
        if status != 'imported':
            raise RuntimeError('An interrupted upload needs review. See docs/media-imports.md.')
        return False
    with file.open('rb') as source:
        response = calibre.post(url + '/upload',
                                data={'csrf_token': token}, files={'btn-upload': (file.name, source)}, timeout=300)
    response.raise_for_status()
    location = response.json().get('location', '')
    match = re.fullmatch(r'/(?:admin/)?book/(\d+)', location)
    if not match:
        raise RuntimeError('Calibre-Web did not confirm a successful book upload')
    with ledger:
        ledger.execute('UPDATE imports SET status=?, book_id=? WHERE sha256=?',
                       ('imported', int(match[1]), digest))
    return True


def import_inbox(storage):
    with closing(open_ledger(LEDGER)) as ledger, requests.Session() as calibre:
        known = dict(ledger.execute('SELECT sha256, status FROM imports'))
        url = None
        imported = 0
        for key in storage.objects(INBOX):
            digest = key.removeprefix(INBOX)
            if not re.fullmatch(r'[0-9a-f]{64}', digest):
                continue
            if digest in known:
                if known[digest] == 'pending':
                    print(f'Pending Calibre-Web upload {digest}. See docs/media-imports.md.', flush=True)
                continue
            info = storage.head(key)
            if not info or info.get('x-amz-meta-sha256') != digest:
                raise RuntimeError('An inbox object has invalid SHA-256 metadata')
            if not 0 < int(info['content-length']) <= MAX_BOOK_BYTES:
                raise RuntimeError('An inbox object is outside the book size limit')
            name = unquote(info.get('x-amz-meta-filename', ''))
            if (Path(name).name != name or '\\' in name or '\0' in name
                    or Path(name).suffix.lower() not in BOOK_FORMATS):
                raise RuntimeError('An inbox object has an invalid book filename')
            with tempfile.TemporaryDirectory() as temporary:
                file = Path(temporary) / name
                storage.download(key, file, digest)
                if url is None:
                    url = connect_calibre(calibre)
                imported += import_book(calibre, url, ledger, file, digest)
        if imported:
            print(f'Imported {imported} new book file(s) from OVH.', flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=('torrent', 'usenet', 'usenet-stage', 'inbox'))
    args = parser.parse_args()
    storage = BookStorage(json.loads(BUCKET_CONFIG.read_text()))
    if args.mode == 'torrent':
        hashes = {put_book(storage, file) for file in torrent_files()}
        print(f'Verified {len(hashes)} completed torrent book file(s) in OVH.')
        return
    if args.mode in ('usenet', 'usenet-stage'):
        from usenet_cart import poll
        if args.mode == 'usenet-stage':
            poll(storage, stage=True)
            return
        work = poll
    else:
        work = import_inbox
    while True:
        try:
            work(storage)
        except (requests.RequestException, RuntimeError, OSError, ValueError, sqlite3.Error) as error:
            # Paths and HTTP exceptions can include account data.
            details = str(error) if type(error) is RuntimeError else type(error).__name__
            print(f'Books synchronization failed: {details}', flush=True)
        time.sleep(120)


if __name__ == '__main__':
    main()
