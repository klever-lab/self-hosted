"""Use curl's S3 Signature Version 4 support for the private Books bucket."""

import hashlib
import json
import re
import subprocess
from urllib.parse import quote, urlencode
from xml.etree import ElementTree


class BookStorage:
    def __init__(self, config):
        self.bucket = config['bucket']
        self.region = config['region']
        self.auth = config['access_key'] + ':' + config['secret_key']
        if not re.fullmatch(r'[a-z0-9][a-z0-9-]{1,61}[a-z0-9]', self.bucket):
            raise ValueError('Expected a DNS-compatible Books bucket name')
        if not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,19}', self.region):
            raise ValueError('Expected an OVH object storage region')
        if not re.fullmatch(r'[A-Za-z0-9_-]+:[!-~]+', self.auth):
            raise ValueError('Invalid Books S3 credentials')
        self.url = f'https://s3.{self.region}.io.cloud.ovh.net/{self.bucket}'

    def request(self, method, key='', *, params=None, source=None, destination=None, headers=(), accept=(200,)):
        url = self.url + '/' + quote(key, safe='/') if key else self.url
        if params:
            url += '?' + urlencode(params, quote_via=quote)
        commands = ['curl', '--disable', '--silent', '--show-error', '--noproxy', '*',
                    '--proto', '=https', '--connect-timeout', '10', '--max-time', '3600',
                    '--speed-limit', '128', '--speed-time', '60',
                    '--aws-sigv4', f'aws:amz:{self.region}:s3', '--config', '-',
                    '--write-out', '\n%{http_code}']
        for header in headers:
            commands.extend(['--header', header])
        if method == 'HEAD':
            commands.append('--head')
        elif source is not None:
            commands.extend(['--upload-file', str(source)])
        if destination is not None:
            commands.extend(['--output', str(destination)])
        result = subprocess.run([*commands, '--url', url], input='user = ' + json.dumps(self.auth) + '\n',
                                encoding='utf-8', errors='replace', capture_output=True)
        if result.returncode:
            raise RuntimeError(f'Books S3 {method} transport failed (curl {result.returncode})')
        body, _, status = result.stdout.rpartition('\n')
        if not status.isdigit() or int(status) not in accept:
            raise RuntimeError(f'Books S3 {method} returned HTTP {status if status.isdigit() else "unknown"}')
        return int(status), body

    def objects(self, prefix):
        params = {'list-type': '2', 'prefix': prefix}
        while True:
            _, body = self.request('GET', params=params)
            document = ElementTree.fromstring(body)
            if document.tag.rsplit('}', 1)[-1] != 'ListBucketResult':
                raise RuntimeError('Books S3 did not return a bucket listing')
            yield from (object_key.text for object_key in document.findall('{*}Contents/{*}Key'))
            if document.findtext('{*}IsTruncated') != 'true':
                return
            token = document.findtext('{*}NextContinuationToken')
            if not token or token == params.get('continuation-token'):
                raise RuntimeError('Books S3 listing did not advance')
            params['continuation-token'] = token

    def head(self, key):
        code, body = self.request('HEAD', key, accept=(200, 404))
        if code == 404:
            return None
        return {name.lower(): value.strip() for line in body.splitlines()
                if ':' in line for name, value in (line.split(':', 1),)}

    def put(self, key, file, digest, *, name=None):
        if file.stat().st_size > 5 * 1024**3:
            raise RuntimeError('Books S3 needs multipart upload for objects above 5 GiB')
        stored = self.head(key)
        if stored is None:
            headers = ['If-None-Match: *', f'x-amz-content-sha256: {digest}', f'x-amz-meta-sha256: {digest}']
            if name:
                headers.append('x-amz-meta-filename: ' + quote(name, safe=''))
            self.request('PUT', key, source=file, headers=headers, accept=(200, 201, 204, 412))
            stored = self.head(key)
        if (not stored or stored.get('x-amz-meta-sha256') != digest
                or stored.get('content-length') != str(file.stat().st_size)):
            raise RuntimeError('Books S3 could not verify the stored object')

    def download(self, key, destination, digest):
        self.request('GET', key, destination=destination)
        if sha256(destination) != digest:
            raise RuntimeError('A Books S3 download did not match its SHA-256')


def sha256(path):
    with path.open('rb') as source:
        return hashlib.file_digest(source, 'sha256').hexdigest()
