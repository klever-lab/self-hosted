"""Upload completed books through Calibre-Web; keep all seeding files unchanged."""

import hashlib
from html.parser import HTMLParser
import os
from pathlib import Path
import re
import sqlite3

import requests


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


def main():
    qbt_url = os.environ['QBITTORRENT_URL'].rstrip('/')
    calibre_url = os.environ['CALIBRE_URL'].rstrip('/')
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
    if not torrents:
        print('No completed book downloads to import.')
        return

    ledger = sqlite3.connect('/state/imports.sqlite3')
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
    for torrent in torrents:
        response = qbt.get(qbt_url + '/api/v2/torrents/files', params={'hash': torrent['hash']}, timeout=30)
        response.raise_for_status()
        for item in response.json():
            if item['progress'] != 1:
                continue
            file = (Path(torrent['save_path']) / item['name']).resolve()
            if not file.is_relative_to(Path('/downloads')):
                raise RuntimeError('A torrent file resolves outside the download volume')
            if file.suffix.lower() not in {'.epub', '.kepub', '.pdf', '.mobi', '.azw3', '.fb2'}:
                continue
            if not file.is_file():
                raise RuntimeError('A completed book file is missing')
            if file.stat().st_size > 200 * 1024 * 1024:
                raise RuntimeError('A book exceeds the 200 MiB upload limit')
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
    print(f'Imported {imported} new book file(s); seeding originals preserved.')


if __name__ == '__main__':
    main()
