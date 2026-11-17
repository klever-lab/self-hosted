"""Search altHUB, inspect NZB filenames, and manage individual cart entries."""

import argparse
import json
from pathlib import Path
import re
import sys
from urllib.parse import quote, quote_plus, urljoin, urlsplit
from xml.etree import ElementTree

import requests

import manage

API = 'https://api.althub.co.za/api'
HOSTS = {'api.althub.co.za', 'althub.co.za'}
CATEGORIES = {'ebook': '7020', 'books': '7000', 'all': None}
FORMATS = {'.epub', '.kepub', '.mobi', '.azw3', '.pdf', '.fb2'}


class AltHubError(RuntimeError):
    def __init__(self, message, *, code=None):
        super().__init__(message)
        self.code = code


def release_id(value):
    if not re.fullmatch(r'[0-9a-fA-F]{32}', value):
        raise argparse.ArgumentTypeError('Use the 32-character release ID from search output.')
    return value.lower()


def integer(value, minimum, maximum=None):
    try:
        result = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError('Expected an integer.') from None
    if result < minimum or (maximum is not None and result > maximum):
        raise argparse.ArgumentTypeError(f'Expected a value from {minimum} to {maximum}.' if maximum is not None
                                         else f'Expected a value of at least {minimum}.')
    return result


class AltHub:
    def __init__(self, api_key, session):
        self.api_key = api_key
        self.session = session
        self.session.max_redirects = 3
        self.session.hooks['response'].append(self.check_redirect)

    def redact(self, text):
        for token in (self.api_key, quote(self.api_key, safe=''), quote_plus(self.api_key)):
            text = text.replace(token, '[redacted]')
        return re.sub(r"https?://[^\s<>\"']+", '[private URL]', text)

    @staticmethod
    def check_redirect(response, **kwargs):
        if response.is_redirect:
            try:
                target = urlsplit(urljoin(response.url, response.headers['Location']))
                trusted = target.scheme == 'https' and target.hostname in HOSTS and not target.username and target.port in (None, 443)
            except ValueError:
                trusted = False
            if not trusted:
                raise AltHubError('altHUB redirected outside its trusted HTTPS hosts.')

    def request(self, operation, **params):
        try:
            response = self.session.get(API, params={'t': operation, 'apikey': self.api_key, 'o': 'xml', **params},
                                        timeout=(10, 30), allow_redirects=operation == 'get')
        except requests.Timeout:
            raise AltHubError('altHUB request timed out.') from None
        except requests.RequestException:
            # Requests exceptions can contain credential-bearing URLs.
            raise AltHubError('altHUB connection failed.') from None
        if response.status_code == 429:
            raise AltHubError('altHUB rate limit reached; retry later.')
        if response.status_code != 200:
            raise AltHubError(f'altHUB returned HTTP {response.status_code}.')
        try:
            root = ElementTree.fromstring(response.content)
        except ElementTree.ParseError:
            raise AltHubError('altHUB returned invalid XML.') from None
        if root.tag == 'error':
            raw_code = root.get('code', '')
            code = raw_code if raw_code.isdigit() else 'unknown'
            description = self.redact(root.get('description', 'Request rejected.'))[:200]
            raise AltHubError(f'altHUB API error {code}: {description}', code=code)
        return root

    def search(self, query, *, category='ebook', limit=20, offset=0):
        params = {'q': query, 'limit': limit, 'offset': offset}
        if CATEGORIES[category]:
            params['cat'] = CATEGORIES[category]
        root = self.request('search', **params)
        if root.tag != 'rss' or root.find('channel') is None:
            raise AltHubError('altHUB did not return a search feed.')
        results = []
        for item in root.findall('./channel/item'):
            attrs = {node.get('name'): node.get('value') for node in item.findall('{*}attr')}
            try:
                identifier = attrs.get('guid') or urlsplit(item.findtext('guid', '')).path.rsplit('/', 1)[-1]
                identifier = release_id(identifier)
                enclosure = item.find('enclosure')
                size = int(attrs.get('size') or (enclosure.get('length') if enclosure is not None else 0) or 0)
            except (argparse.ArgumentTypeError, ValueError):
                raise AltHubError('altHUB returned an invalid release ID or size.') from None
            # Never forward API links: altHUB embeds private download tokens in URL paths.
            results.append({'id': identifier, 'title': self.redact(item.findtext('title', '')),
                            'size_bytes': size, 'posted': item.findtext('pubDate', ''),
                            'category': item.findtext('category', ''),
                            'details_url': 'https://althub.co.za/details/' + identifier})
        metadata = root.find('./channel/{*}response')
        try:
            total = int(metadata.get('total')) if metadata is not None and metadata.get('total') else None
        except ValueError:
            raise AltHubError('altHUB returned an invalid result count.') from None
        return {'query': self.redact(query), 'category': category, 'offset': offset, 'limit': limit,
                'total': total, 'results': results}

    def files(self, identifier):
        root = self.request('get', id=identifier)
        if root.tag.rsplit('}', 1)[-1] != 'nzb':
            raise AltHubError('altHUB did not return an NZB document.')
        files = []
        for item in root.findall('{*}file'):
            subject = self.redact(item.get('subject', ''))
            quoted = re.findall(r'"([^"]+)"', subject)
            name = quoted[-1] if quoted else None
            suffix = Path(name).suffix.lower() if name else ''
            if name and name.lower().endswith('.kepub.epub'):
                kind = 'kepub'
            else:
                kind = suffix.lstrip('.') if suffix in FORMATS else None
            try:
                size = sum(int(node.get('bytes', '0')) for node in item.findall('./{*}segments/{*}segment'))
            except ValueError:
                raise AltHubError('altHUB returned an invalid NZB segment size.') from None
            files.append({'name': name, 'format': kind, 'size_bytes': size, 'subject': subject})
        if not files:
            raise AltHubError('altHUB returned an NZB with no files.')
        return {'id': identifier, 'formats': sorted({item['format'] for item in files if item['format']}), 'files': files}

    def cart(self, action, identifier):
        operation = {'add': 'cartadd', 'remove': 'cartdel'}[action]
        try:
            root = self.request(operation, id=identifier)
        except AltHubError as error:
            if action == 'add' and error.code == '310':
                return {'id': identifier, 'status': 'already_present'}
            raise
        if root.tag != operation:
            raise AltHubError('altHUB did not confirm the cart change.')
        return {'id': identifier, 'status': 'added' if action == 'add' else 'removed'}


def load_api_key():
    if manage.STATE.is_relative_to(manage.ROOT):
        raise AltHubError('Operator storage must be outside the checkout.')
    try:
        values = manage.load('usenet.json')
    except (OSError, ValueError):
        raise AltHubError('Could not read valid private operator credentials.') from None
    key = values.get('althub_api_key') if isinstance(values, dict) else None
    if not isinstance(key, str) or not key.strip():
        raise AltHubError('Save althub_api_key in the protected operator usenet.json first.')
    return key.strip()


def main(argv=None):
    parser = argparse.ArgumentParser(prog='althub', description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    search = commands.add_parser('search', help='Search one page; defaults to ebooks.')
    search.add_argument('query', nargs='+')
    search.add_argument('--category', choices=CATEGORIES, default='ebook')
    search.add_argument('--limit', type=lambda value: integer(value, 1, 100), default=20)
    search.add_argument('--offset', type=lambda value: integer(value, 0), default=0)
    files = commands.add_parser('files', help='Inspect NZB file subjects and formats; counts as an NZB grab.')
    files.add_argument('id', type=release_id)
    cart = commands.add_parser('cart', help='Add or remove a single cart entry.')
    actions = cart.add_subparsers(dest='action', required=True)
    for action in ('add', 'remove'):
        actions.add_parser(action).add_argument('id', type=release_id)
    args = parser.parse_args(argv)
    try:
        key = load_api_key()
        with requests.Session() as session:
            client = AltHub(key, session)
            if args.command == 'search':
                result = client.search(' '.join(args.query), category=args.category, limit=args.limit, offset=args.offset)
            elif args.command == 'files':
                result = client.files(args.id)
            else:
                result = client.cart(args.action, args.id)
        print(json.dumps(result, indent=2))
    except AltHubError as error:
        print(json.dumps({'error': str(error), 'code': error.code}), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
