"""Create a native Calibre library and configure Calibre-Web before exposure."""

import os
from pathlib import Path
import shutil
import sys

os.environ['CALIBRE_DBPATH'] = '/config'
os.environ['CALIBRE_CONFIG_DIRECTORY'] = '/config/calibre'
# Calibre-Web 0.6.27 recognizes the platform filename, not the image's generic one.
kepubify = Path('/tools/kepubify-linux-64bit')
shutil.copyfile('/opt/kepubify/kepubify', kepubify)
kepubify.chmod(0o755)
if not Path('/books/metadata.db').exists():
    shutil.copyfile('/bootstrap/empty-library.db', '/books/metadata.db')

sys.path.insert(0, '/app')
from cps import config_sql, constants, ub
from werkzeug.security import generate_password_hash

fresh = not Path('/config/app.db').exists()
ub.init_db('/config/app.db')
key, error = config_sql.get_encryption_key('/config')
if error:
    raise RuntimeError('Cannot initialize Calibre-Web encryption key')
config_sql.load_configuration(ub.session, key)
settings = ub.session.query(config_sql._Settings).one()
for field, value in {
    'config_calibre_dir': '/books',
    'config_port': 8083,
    'config_external_port': 443,
    'config_calibre_web_title': 'K Books',
    'config_uploading': 1,
    'config_anonbrowse': 0,
    'config_public_reg': 0,
    'config_kobo_sync': True,
    'config_kobo_proxy': False,
    'config_binariesdir': '',
    'config_converterpath': '',
    'config_kepubifypath': '/tools',
    'config_default_role': constants.ROLE_DOWNLOAD | constants.ROLE_VIEWER | constants.ROLE_PASSWD | constants.ROLE_EDIT_SHELFS,
    'config_access_log': 0,
}.items():
    setattr(settings, field, value)

if fresh:
    admin = ub.session.query(ub.User).filter_by(name='admin').one()
    admin.name = Path('/credentials/username').read_text().strip()
    admin.password = generate_password_hash(Path('/credentials/password').read_text().strip())
    admin.email = 'admin@localhost'

ingest = ub.session.query(ub.User).filter_by(name='book-importer').one_or_none()
if ingest is None:
    ingest = ub.User()
    ingest.name = 'book-importer'
    ingest.email = 'importer@localhost'
    ub.session.add(ingest)
ingest.password = generate_password_hash(Path('/credentials/import-password').read_text().strip())
ingest.role = constants.ROLE_UPLOAD
ub.session.commit()
print('Calibre library initialized; authentication, uploads and Kobo sync enabled.')
