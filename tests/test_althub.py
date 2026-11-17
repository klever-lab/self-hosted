"""Exercise the CLI through Requests without contacting an indexer or using secrets."""

from contextlib import redirect_stderr, redirect_stdout
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
SPEC = importlib.util.spec_from_file_location('althub_client', ROOT / 'scripts/althub.py')
cli = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cli)
KEY = 'a' * 32
ID = 'b' * 32
DOWNLOAD_TOKEN = 'fixture-download-token'

SEARCH = f'''<rss xmlns:n="http://www.newznab.com/DTD/2010/feeds/attributes/">
  <channel><n:response offset="20" total="21"/><item>
    <title>I, Robot</title><guid>https://api.althub.co.za/details/{ID}</guid>
    <link>https://api.althub.co.za/getnzb/{ID}.nzb&amp;i=1&amp;r={DOWNLOAD_TOKEN}</link>
    <enclosure url="https://api.althub.co.za/private/{DOWNLOAD_TOKEN}" length="123"/>
    <category>Books &gt; Ebook</category><pubDate>Wed, 10 Jan 2018 00:47:58 +0000</pubDate>
    <n:attr name="guid" value="{ID}"/><n:attr name="size" value="234399"/>
  </item></channel></rss>'''
NZB = '''<nzb xmlns="http://www.newzbin.com/DTD/2003/nzb">
  <file subject='Book - "I, Robot.epub" yEnc'><segments><segment bytes="3">one</segment><segment bytes="5">two</segment></segments></file>
  <file subject='Book - "I, Robot.mobi" yEnc'><segments><segment bytes="7">three</segment></segments></file>
</nzb>'''


def response(body='', *, status=200, headers=None):
    return status, body.encode(), headers or {}


class MemoryAdapter(requests.adapters.BaseAdapter):
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def send(self, request, **kwargs):
        self.calls.append(request)
        if not self.replies:
            raise AssertionError('Unexpected HTTP request')
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        status, body, headers = reply
        result = requests.Response()
        result.status_code = status
        result.url = request.url
        result.request = request
        result.headers.update(headers)
        result._content = body
        return result

    def close(self):
        pass


class ClientTests(unittest.TestCase):
    def client(self, *replies):
        session = requests.Session()
        self.addCleanup(session.close)
        adapter = MemoryAdapter(replies)
        session.mount('https://', adapter)
        return cli.AltHub(KEY, session), adapter

    def test_search_uses_requests_encoding_and_explicit_pagination_without_exposing_download_urls(self):
        client, adapter = self.client(response(SEARCH))
        result = client.search('Asimov & Robot', limit=1, offset=20)
        params = parse_qs(urlsplit(adapter.calls[0].url).query)
        self.assertEqual(params, {'t': ['search'], 'apikey': [KEY], 'o': ['xml'],
                                 'q': ['Asimov & Robot'], 'cat': ['7020'], 'limit': ['1'], 'offset': ['20']})
        self.assertEqual(result['total'], 21)
        self.assertEqual(result['results'][0]['id'], ID)
        self.assertEqual(result['results'][0]['size_bytes'], 234399)
        self.assertEqual(result['results'][0]['details_url'], 'https://althub.co.za/details/' + ID)
        output = json.dumps(result)
        self.assertNotIn(KEY, output)
        self.assertNotIn(DOWNLOAD_TOKEN, output)
        self.assertNotIn('getnzb', output)

    def test_guid_url_fallback_and_empty_results(self):
        feed = SEARCH.replace(f'<n:attr name="guid" value="{ID}"/>', '')
        client, _ = self.client(response(feed), response('<rss><channel/></rss>'))
        self.assertEqual(client.search('robot')['results'][0]['id'], ID)
        empty = client.search('missing', category='all')
        self.assertEqual(empty['results'], [])
        self.assertIsNone(empty['total'])

    def test_invalid_release_metadata_is_reported_instead_of_dropping_results(self):
        malformed_guid = SEARCH.replace(f'<n:attr name="guid" value="{ID}"/>', '').replace(
            f'<guid>https://api.althub.co.za/details/{ID}</guid>', '<guid>https://[invalid</guid>')
        for feed in (malformed_guid, SEARCH.replace('value="234399"', 'value="invalid"'),
                     SEARCH.replace('total="21"', 'total="invalid"')):
            with self.subTest(feed=feed):
                client, _ = self.client(response(feed))
                with self.assertRaises(cli.AltHubError):
                    client.search('robot')

    def test_nzb_inspection_follows_trusted_redirect_and_reads_namespace_and_formats(self):
        client, adapter = self.client(response(status=302, headers={
            'Location': f'/getnzb/{ID}.nzb&i=1&r={DOWNLOAD_TOKEN}'}), response(NZB))
        result = client.files(ID)
        self.assertEqual(len(adapter.calls), 2)
        self.assertEqual(result['formats'], ['epub', 'mobi'])
        self.assertEqual(result['files'][0]['name'], 'I, Robot.epub')
        self.assertEqual(result['files'][0]['size_bytes'], 8)
        self.assertNotIn(DOWNLOAD_TOKEN, json.dumps(result))
        self.assertNotIn('del', parse_qs(urlsplit(adapter.calls[0].url).query))

    def test_redirects_cannot_send_credentials_to_external_or_insecure_addresses(self):
        for location in ('https://external.example/private', 'http://api.althub.co.za/private',
                         'https://api.althub.co.za:444/private', 'https://user@api.althub.co.za/private',
                         'https://api.althub.co.za:invalid/private'):
            with self.subTest(location=location):
                client, adapter = self.client(response(status=302, headers={'Location': location}))
                with self.assertRaisesRegex(cli.AltHubError, 'trusted HTTPS'):
                    client.files(ID)
                self.assertEqual(len(adapter.calls), 1)

    def test_unquoted_subject_is_not_misrepresented_as_a_verified_filename(self):
        nzb = '<nzb><file subject="unparsed subject mentioning epub"/><file subject=\'"book.kepub.epub"\'/></nzb>'
        client, _ = self.client(response(nzb))
        result = client.files(ID)
        self.assertIsNone(result['files'][0]['name'])
        self.assertEqual(result['formats'], ['kepub'])

    def test_cart_add_duplicate_and_remove_use_the_release_id_parameter(self):
        client, adapter = self.client(response('<cartadd id="42"/>'),
            response('<error code="310" description="Item already exists"/>'), response('<cartdel id="42"/>'))
        self.assertEqual(client.cart('add', ID)['status'], 'added')
        self.assertEqual(client.cart('add', ID)['status'], 'already_present')
        self.assertEqual(client.cart('remove', ID)['status'], 'removed')
        for request, operation in zip(adapter.calls, ('cartadd', 'cartadd', 'cartdel')):
            params = parse_qs(urlsplit(request.url).query)
            self.assertEqual(params['id'], [ID])
            self.assertEqual(params['t'], [operation])
            self.assertNotIn('guid', params)

    def test_api_errors_redact_both_api_key_and_separate_download_token(self):
        client, _ = self.client(response(f'<error code="100" description="Key {KEY}; https://api.althub.co.za/private/{DOWNLOAD_TOKEN}"/>'))
        with self.assertRaises(cli.AltHubError) as caught:
            client.search('robot')
        self.assertEqual(caught.exception.code, '100')
        self.assertNotIn(KEY, str(caught.exception))
        self.assertNotIn(DOWNLOAD_TOKEN, str(caught.exception))

    def test_rate_limits_and_connection_errors_are_not_retried_or_reported_as_empty_searches(self):
        for reply in (response(status=429), requests.Timeout('private ' + KEY), requests.ConnectionError('private ' + KEY)):
            with self.subTest(reply=type(reply).__name__):
                client, adapter = self.client(reply)
                with self.assertRaises(cli.AltHubError) as caught:
                    client.search('robot')
                self.assertEqual(len(adapter.calls), 1)
                self.assertNotIn(KEY, str(caught.exception))

    def test_invalid_responses_cannot_report_a_successful_search_cart_change_or_inspection(self):
        cases = [('<html/>', lambda c: c.search('robot')), ('not XML', lambda c: c.files(ID)),
                 ('<nzb/>', lambda c: c.files(ID)), ('<rss/>', lambda c: c.cart('add', ID)),
                 ('<error code="300" description="No such GUID"/>', lambda c: c.cart('remove', ID))]
        for body, call in cases:
            with self.subTest(body=body):
                client, _ = self.client(response(body))
                with self.assertRaises(cli.AltHubError):
                    call(client)


class CommandTests(unittest.TestCase):
    def test_cli_prints_json_and_errors_to_stderr_without_leaking_secrets(self):
        for body, expected in ((SEARCH, 0), (f'<error code="100" description="Bad key {KEY}"/>', 1)):
            with self.subTest(expected=expected), requests.Session() as session:
                session.mount('https://', MemoryAdapter([response(body)]))
                output, errors = io.StringIO(), io.StringIO()
                with patch.object(cli, 'load_api_key', return_value=KEY), \
                        patch.object(cli.requests, 'Session', return_value=session), \
                        redirect_stdout(output), redirect_stderr(errors):
                    status = cli.main(['search', 'robot'])
                self.assertEqual(status, expected)
                payload = json.loads(errors.getvalue() if expected else output.getvalue())
                self.assertIn('error' if expected else 'results', payload)
                self.assertNotIn(KEY, output.getvalue() + errors.getvalue())

    def test_private_credentials_are_reused_and_public_permissions_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            path = state / 'usenet.json'
            path.write_text(json.dumps({'althub_api_key': KEY}))
            path.chmod(0o600)
            with patch.object(cli.manage, 'STATE', state):
                self.assertEqual(cli.load_api_key(), KEY)
                path.chmod(0o644)
                with self.assertRaisesRegex(cli.AltHubError, 'private operator'):
                    cli.load_api_key()

    def test_bad_identifiers_limits_and_offsets_fail_before_opening_credentials(self):
        for args in (['cart', 'add', 'invalid'], ['search', 'book', '--limit', '101'],
                     ['search', 'book', '--offset', '-1'], ['files', 'https://private.example/' + KEY]):
            with self.subTest(args=args), patch.object(cli, 'load_api_key') as load:
                errors = io.StringIO()
                with redirect_stderr(errors), self.assertRaises(SystemExit) as caught:
                    cli.main(args)
                self.assertEqual(caught.exception.code, 2)
                self.assertNotIn(KEY, errors.getvalue())
                load.assert_not_called()


if __name__ == '__main__':
    unittest.main()
