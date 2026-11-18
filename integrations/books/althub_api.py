"""Read altHUB releases and manage individual cart entries."""

import argparse
from pathlib import Path
import re
from urllib.parse import parse_qs, quote, quote_plus, urlencode, urljoin, urlsplit, urlunsplit
from xml.etree import ElementTree

import requests

API = 'https://api.althub.co.za/api'
HOSTS = {'api.althub.co.za', 'althub.co.za'}
CATEGORIES = {'ebook': '7020', 'books': '7000', 'all': None}
FORMATS = {'.epub', '.kepub', '.mobi', '.azw3', '.pdf', '.fb2'}


class AltHubError(RuntimeError):
    def __init__(self, message, *, code=None):
        super().__init__(message)
        self.code = code


def cart_feed(url):
    parsed = urlsplit(url)
    params = parse_qs(parsed.query)
    if (parsed.scheme != 'https' or parsed.hostname not in HOSTS or parsed.username
            or parsed.port not in (None, 443) or parsed.path.rstrip('/') != '/rss'
            or params.get('t') != ['-2'] or not params.get('i') or not params.get('r')):
        raise ValueError('Expected an authenticated altHUB cart RSS URL')
    params['del'] = ['0']
    params['dl'] = ['0']
    return urlunsplit(('https', parsed.hostname, parsed.path, urlencode(params, doseq=True), ''))


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

    def cart_items(self, url):
        try:
            response = self.session.get(cart_feed(url), timeout=(10, 30), allow_redirects=False)
            if response.status_code != 200:
                raise AltHubError(f'altHUB cart returned HTTP {response.status_code}.')
            feed = ElementTree.fromstring(response.content)
            channel = feed.find('channel')
            if feed.tag != 'rss' or channel is None:
                raise AltHubError('altHUB did not return a cart RSS feed.')
            return {release_id(urlsplit(item.findtext('guid', '')).path.rsplit('/', 1)[-1]):
                    item.findtext('title', '') for item in channel.findall('item')}
        except (requests.RequestException, ValueError, ElementTree.ParseError, argparse.ArgumentTypeError):
            raise AltHubError('Could not read the altHUB cart RSS feed.') from None

    def nzb(self, identifier):
        root = self.request('get', id=identifier)
        if root.tag.rsplit('}', 1)[-1] != 'nzb' or not root.findall('{*}file'):
            raise AltHubError('altHUB did not return a populated NZB document.')
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
        root = self.nzb(identifier)
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
