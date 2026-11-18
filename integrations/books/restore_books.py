"""Restore a verified Books snapshot into empty local volumes."""

import json
from pathlib import Path, PurePosixPath
import re
import sqlite3
import sys
import tarfile
import tempfile

from book_storage import BookStorage, sha256
from import_books import BUCKET_CONFIG, open_ledger


def sqlite_check(path, table):
    if not path.is_file():
        raise RuntimeError('Books snapshot is missing a required database')
    with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True) as database:
        if database.execute('PRAGMA quick_check').fetchall() != [('ok',)]:
            raise RuntimeError('Books snapshot contains a damaged database')
        return database.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]


def archive_member(member, destination):
    if (member.name.split('/', 1)[0] not in ('config', 'library') or '..' in PurePosixPath(member.name).parts
            or not (member.isdir() or member.isfile())):
        raise RuntimeError('Unexpected file in Books snapshot')
    return tarfile.data_filter(member, destination)


def restore(storage, name, data):
    if not re.fullmatch(r'[0-9]{8}T[0-9]{6}Z-[0-9a-f]{12}', name):
        raise ValueError('Invalid Books snapshot name')
    if any(any((data / volume).iterdir()) for volume in ('config', 'library')):
        raise RuntimeError('Books restore requires empty destination volumes')
    key = 'backups/' + name
    with tempfile.TemporaryDirectory() as temporary:
        work = Path(temporary)
        metadata = work / 'manifest.json'
        manifest_info = storage.head(key + '.json')
        manifest_sha = manifest_info and manifest_info.get('x-amz-meta-sha256', '')
        if not manifest_sha or not re.fullmatch(r'[0-9a-f]{64}', manifest_sha):
            raise RuntimeError('Books snapshot has no verified manifest')
        storage.download(key + '.json', metadata, manifest_sha)
        manifest = json.loads(metadata.read_text())
        digest = manifest['archive_sha256']
        if not re.fullmatch(r'[0-9a-f]{64}', digest) or not name.endswith(digest[:12]):
            raise RuntimeError('Books snapshot archive did not match its name')
        archive = work / 'books.tar.gz'
        storage.download(key + '.tar.gz', archive, digest)
        with tarfile.open(archive, 'r:gz') as source:
            source.extractall(data, filter=archive_member)
    books = sqlite_check(data / 'library/metadata.db', 'books')
    users = sqlite_check(data / 'config/app.db', 'user')
    if (books, users) != tuple(manifest['counts']):
        raise RuntimeError('Restored catalog or account count differs from the snapshot')
    ledger_dir = data / 'config/inbox'
    ledger_dir.mkdir(mode=0o700, exist_ok=True)
    with open_ledger(ledger_dir / 'imports.sqlite3') as ledger:
        ledger.executemany("""
            INSERT INTO imports VALUES (?, ?, NULLIF(?, 0))
            ON CONFLICT(sha256) DO UPDATE SET status=excluded.status, book_id=excluded.book_id
            WHERE imports.status='pending' AND excluded.status='imported'
            """, manifest['import_receipts'])
    print(f'Restored {books} books and {users} accounts from verified OVH snapshot {name}.')


if __name__ == '__main__':
    try:
        restore(BookStorage(json.loads(BUCKET_CONFIG.read_text())), sys.argv[1], Path('/data'))
    except (RuntimeError, OSError, ValueError, KeyError, sqlite3.Error, tarfile.TarError) as error:
        raise SystemExit(str(error) if type(error) is RuntimeError else type(error).__name__) from None
