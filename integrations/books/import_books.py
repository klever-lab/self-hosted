"""Upload completed book downloads through Calibre-Web; retain the originals."""

from contextlib import closing
import hashlib
from html.parser import HTMLParser
import os
from pathlib import Path
import re
import sqlite3

import requests

DOWNLOADS = Path('/downloads')
LEDGER = Path('/state/imports.sqlite3')
BOOK_FORMATS = {'.epub', '.kepub', '.pdf', '.mobi', '.azw3', '.fb2'}


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
    if file.stat().st_size > 200 * 1024 * 1024:
        raise RuntimeError('A book exceeds the 200 MiB upload limit')
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


def usenet_files():
    url = os.environ['SABNZBD_URL'].rstrip('/')
    root = DOWNLOADS / 'complete/books'
    start = 0
    while True:
        # POST keeps the API key out of URLs, access logs and HTTP error messages.
        response = requests.post(url + '/api', data={
            'mode': 'history', 'output': 'json', 'apikey': read_secret('sab-api-key'),
            'start': start, 'limit': 100, 'cat_filter': 'books',
        }, timeout=30)
        response.raise_for_status()
        payload = response.json()
        if 'history' not in payload:
            raise RuntimeError('SABnzbd did not return its download history')
        history = payload['history']
        slots = history['slots']
        for job in slots:
            if job['status'] != 'Completed' or job['category'] != 'books':
                continue
            if not job.get('storage'):
                raise RuntimeError('A completed Usenet job has no storage path')
            directory = Path(job['storage']).resolve()
            if not directory.is_relative_to(root.resolve()) or directory == root.resolve():
                raise RuntimeError('A Usenet job resolves outside its book directory')
            if not directory.exists():
                raise RuntimeError('A completed Usenet download is missing')
            paths = directory.rglob('*') if directory.is_dir() else (directory,)
            for path in paths:
                file = book_file(path, root)
                if file:
                    yield file
        start += len(slots)
        if not slots or start >= int(history['noofslots']):
            break


def main():
    calibre_url = os.environ['CALIBRE_URL'].rstrip('/')
    files = list(usenet_files() if os.environ.get('SABNZBD_URL') else torrent_files())
    if not files:
        print('No completed book downloads to import.')
        return

    with closing(sqlite3.connect(LEDGER)) as ledger:
        ledger.execute('CREATE TABLE IF NOT EXISTS imports (sha256 TEXT PRIMARY KEY, status TEXT NOT NULL, book_id INTEGER)')
        calibre = requests.Session()
        token = csrf(calibre.get(calibre_url + '/login', timeout=30))
        response = calibre.post(calibre_url + '/login', data={
            'username': 'book-importer', 'password': read_secret('calibre-password'), 'csrf_token': token
        }, timeout=30)
        response.raise_for_status()
        if '/login' in response.url:
            raise RuntimeError('Calibre-Web importer authentication failed')

        imported = 0
        for file in files:
            with file.open('rb') as source:
                digest = hashlib.file_digest(source, 'sha256').hexdigest()
            existing = ledger.execute('SELECT status FROM imports WHERE sha256=?', (digest,)).fetchone()
            if existing:
                if existing[0] != 'imported':
                    raise RuntimeError('An interrupted upload needs review; see docs/media-imports.md')
                continue
            token = csrf(calibre.get(calibre_url + '/', timeout=30))
            # Commit intent first. An interrupted response must not silently create duplicates.
            ledger.execute('INSERT INTO imports VALUES (?, ?, NULL)', (digest, 'pending'))
            ledger.commit()
            with file.open('rb') as source:
                response = calibre.post(calibre_url + '/upload',
                    data={'csrf_token': token}, files={'btn-upload': (file.name, source)}, timeout=300)
            response.raise_for_status()
            location = response.json().get('location', '')
            match = re.fullmatch(r'/(?:admin/)?book/(\d+)', location)
            if not match:
                raise RuntimeError('Calibre-Web did not confirm a successful book upload')
            ledger.execute('UPDATE imports SET status=?, book_id=? WHERE sha256=?',
                           ('imported', int(match[1]), digest))
            ledger.commit()
            imported += 1
        print(f'Imported {imported} new book file(s); downloaded originals preserved.')


if __name__ == '__main__':
    main()
