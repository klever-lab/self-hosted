"""Initialize the downloader without replacing saved provider or indexer settings."""

from pathlib import Path
import os
from urllib.parse import parse_qs, urlsplit


def configure(config, credentials, *, books_inbox=False):
    misc = config.setdefault('misc', {})
    misc.update({
        'host': '0.0.0.0', 'port': 8080, 'url_base': '',
        'username': credentials['username'], 'password': credentials['password'],
        'api_key': credentials['api-key'], 'nzb_key': credentials['nzb-key'],
        'disable_api_key': 0, 'inet_exposure': 5, 'html_login': 1,
        'host_whitelist': ['sabnzbd', 'sabnzbd.sabnzbd.svc.cluster.local', credentials['domain']],
        'download_dir': '/downloads/incomplete', 'complete_dir': '/downloads/complete',
        'admin_dir': '/config/admin', 'log_dir': '/config/logs',
        'auto_browser': 0, 'enable_broadcast': 0, 'api_logging': 0,
    })
    for name, value in {'download_free': '1G', 'cache_limit': '64M', 'direct_unpack': 1}.items():
        misc.setdefault(name, value)
    config.setdefault('categories', {}).setdefault('books', {
        'dir': 'books', 'pp': 3, 'script': 'Default', 'priority': 0, 'newzbin': ['Books*'],
    })
    if books_inbox:
        for feed in config.get('rss', {}).values():
            if not isinstance(feed, dict):
                continue
            urls = feed.get('uri', [])
            for url in ([urls] if isinstance(urls, str) else urls):
                source = urlsplit(url)
                if source.hostname in ('althub.co.za', 'api.althub.co.za') and parse_qs(source.query).get('t') == ['-2']:
                    feed['enable'] = 0
    servers = config.setdefault('servers', {})
    # Disabled until the operator supplies the Usenet login in Config > Servers.
    for host, priority, connections in (
        ('news.frugalusenet.com', 0, 10), ('bonus.frugalusenet.com', 1, 5),
    ):
        servers.setdefault(host, {
            'host': host, 'port': 563, 'ssl': 1, 'ssl_verify': 3,
            'enable': 0, 'username': '', 'password': '',
            'connections': connections, 'priority': priority,
        })
        if credentials.get('frugal-username') and credentials.get('frugal-password'):
            servers[host].update({
                'username': credentials['frugal-username'], 'password': credentials['frugal-password'],
                'enable': 1, 'ssl': 1, 'ssl_verify': 3, 'port': 563,
            })


def main():
    from configobj import ConfigObj

    path = Path('/config/sabnzbd.ini')
    config = ConfigObj(str(path), encoding='utf-8')
    names = ('username', 'password', 'api-key', 'nzb-key', 'domain')
    credentials = {name: Path('/credentials', name).read_text().strip() for name in names}
    for name in ('frugal-username', 'frugal-password'):
        secret = Path('/credentials', name)
        if secret.exists():
            credentials[name] = secret.read_text().strip()
    configure(config, credentials, books_inbox=os.environ.get('BOOKS_INBOX') == '1')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch(mode=0o600, exist_ok=True)
    path.chmod(0o600)
    config.write()
    for directory in ('/downloads/incomplete', '/downloads/complete/books'):
        Path(directory).mkdir(parents=True, exist_ok=True)


if __name__ == '__main__':
    main()
