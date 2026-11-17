"""Set up the Friends VPN hostname and manage its exact-email Access policy."""

import json
import re
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from friends_vpn_inventory import email_address

HOST = 'vpn.bahasadri.com'
ZONE = 'bahasadri.com'
URL = 'https://' + HOST
TUNNEL = 'friends-vpn-guaranine'
APP = 'Friends VPN'
POLICY = 'Friends VPN invited emails'
DISABLED_EMAIL = 'disabled@friends-vpn.invalid'
ORIGIN = 'http://127.0.0.1:8080'
POLICY_FIELDS = ('name', 'decision', 'include', 'exclude', 'require', 'precedence', 'approval_groups',
                 'approval_required', 'connection_rules', 'isolation_required', 'mfa_config',
                 'purpose_justification_prompt', 'purpose_justification_required', 'session_duration')


class CloudflareError(RuntimeError):
    def __init__(self, endpoint, status, errors=()):
        super().__init__(f'Cloudflare {endpoint}: HTTP {status}' +
                         (f' (API error codes {", ".join(errors)})' if errors else ''))
        self.status = status


class Cloudflare:
    def __init__(self, token):
        self.token = token.strip()
        if not self.token or any(ch.isspace() for ch in self.token):
            raise ValueError('Invalid Cloudflare API token file')

    def request(self, method, path, *, body=None, query=None):
        url = 'https://api.cloudflare.com/client/v4' + path
        if query:
            url += '?' + urlencode(query)
        req = Request(url, method=method, data=None if body is None else json.dumps(body).encode(),
                      headers={'Authorization': 'Bearer ' + self.token, 'Content-Type': 'application/json'})
        status = 200
        try:
            with urlopen(req, timeout=25) as response:
                envelope = json.load(response)
                status = response.status
        except HTTPError as error:
            status = error.code
            with error:
                try:
                    envelope = json.load(error)
                except ValueError:
                    envelope = {}
        except URLError as error:
            raise RuntimeError(f'Cloudflare request failed for {path}: {error.reason}') from None
        if status >= 400 or not envelope.get('success'):
            codes = [str(error['code']) for error in envelope.get('errors', []) if isinstance(error.get('code'), int)]
            raise CloudflareError(path, status, codes)
        return envelope

    def call(self, method, path, body=None):
        return self.request(method, path, body=body).get('result')

    def list(self, path, **filters):
        page = 1
        while True:
            response = self.request('GET', path, query={**filters, 'page': page, 'per_page': 50})
            yield from response['result']
            if page >= response.get('result_info', {}).get('total_pages', 1):
                return
            page += 1


def one(items, description):
    found = list(items)
    if len(found) > 1:
        raise ValueError(f'Multiple Cloudflare resources match {description}')
    return found[0] if found else None


def account_path(config):
    account = config['account_id']
    if not re.fullmatch(r'[a-f0-9]{32}', account):
        raise ValueError('Invalid Cloudflare account ID')
    return '/accounts/' + account


def app_path(config):
    app = config['application_id']
    if not re.fullmatch(r'[a-f0-9-]{36}', app):
        raise ValueError('Invalid Cloudflare Access application ID')
    return account_path(config) + '/access/apps/' + app


def policy_emails(policy):
    if policy.get('decision') != 'allow' or not policy.get('include'):
        raise ValueError('Use a dedicated Cloudflare Allow policy with exact email addresses')
    try:
        emails = set()
        for rule in policy['include']:
            if set(rule) != {'email'}:
                raise ValueError('Expected an exact Email selector')
            emails.add(email_address(rule['email']['email']))
        return emails
    except (KeyError, TypeError, ValueError):
        raise ValueError('The Friends VPN Access policy must contain only exact Email include rules') from None


def read_email_policy(api, config):
    app = api.call('GET', app_path(config))
    if app.get('domain') != HOST:
        raise ValueError(f'The configured Cloudflare Access application must protect {HOST}')
    policy_id = config['policy_id']
    if not re.fullmatch(r'[a-f0-9-]{36}', policy_id):
        raise ValueError('Invalid Cloudflare Access policy ID')
    if {policy['id'] for policy in app.get('policies', [])} != {policy_id}:
        raise ValueError('The portal Access application must use its dedicated email allowlist')
    path = account_path(config) + '/access/policies/' + policy_id
    current = api.call('GET', path)
    if current.get('app_count') != 1:
        raise ValueError('The Friends VPN Access policy must be dedicated to its portal')
    return path, current


def change_email(api, config, address, *, allow):
    path, current = read_email_policy(api, config)
    emails = policy_emails(current) - {DISABLED_EMAIL}
    updated = (emails | {address}) if allow else (emails - {address})
    if updated == emails:
        return False
    body = {field: current[field] for field in POLICY_FIELDS if current.get(field) is not None}
    body.pop('precedence', None)
    body['include'] = [{'email': {'email': value}} for value in sorted(updated or {DISABLED_EMAIL})]
    api.call('PUT', path, body)
    return True


def revoke_sessions(api, config, address):
    api.call('POST', account_path(config) + '/access/organizations/revoke_user', {'email': address})


def setup(api):
    zone = one(api.list('/zones', name=ZONE), ZONE)
    if not zone or zone.get('status') != 'active':
        raise ValueError(f'An active Cloudflare DNS zone is required for {ZONE}')
    account_id = zone['account']['id']
    account = account_path({'account_id': account_id})
    domain = api.call('GET', account + '/access/organizations').get('auth_domain', '')
    if not re.fullmatch(r'[a-z0-9-]+\.cloudflareaccess\.com', domain):
        raise ValueError('Set up the Cloudflare Zero Trust team for this account first')
    provider = next((p for p in api.list(account + '/access/identity_providers') if p['type'] == 'onetimepin'), None)
    if not provider:
        raise ValueError('Enable the One-time PIN login method in Cloudflare Zero Trust')
    providers = [provider['id']]
    apps = account + '/access/apps'
    app_settings = {'name': APP, 'domain': HOST, 'type': 'self_hosted', 'allowed_idps': providers,
                    'auto_redirect_to_identity': True, 'session_duration': '8h', 'app_launcher_visible': False,
                    'http_only_cookie_attribute': True}
    policies = account + '/access/policies'
    policy = one((p for p in api.list(policies) if p.get('name') == POLICY), 'the Friends VPN Access allowlist')
    if not policy:
        policy = api.call('POST', policies, {'name': POLICY, 'decision': 'allow',
                                            'include': [{'email': {'email': DISABLED_EMAIL}}]})
    policy_emails(policy)
    app_settings['policies'] = [{'id': policy['id']}]
    app = one((a for a in api.list(apps) if a.get('domain') == HOST), HOST)
    if not app:
        if policy.get('app_count', 0):
            raise ValueError('An existing Friends VPN Access policy is already attached to another application')
        app = api.call('POST', apps, app_settings)
    else:
        app = api.call('GET', apps + '/' + app['id'])
    if app.get('name') != APP or app.get('allowed_idps') != providers or not re.fullmatch(r'[a-f0-9]{64}', app.get('aud', '')):
        raise ValueError(f'An existing Cloudflare Access application for {HOST} needs review')
    attached = {p['id'] for p in app.get('policies', [])}
    if not attached and not policy.get('app_count', 0):
        app = api.call('PUT', apps + '/' + app['id'], app_settings)
    elif attached != {policy['id']} or policy.get('app_count', 0) > 1:
        raise ValueError(f'An existing Cloudflare Access policy for {HOST} needs review')
    app = api.call('GET', apps + '/' + app['id'])
    if {p['id'] for p in app.get('policies', [])} != {policy['id']}:
        raise ValueError('Cloudflare did not attach the Friends VPN email allowlist')

    tunnels = account + '/cfd_tunnel'
    tunnel = one(api.list(tunnels, name=TUNNEL, is_deleted='false'), TUNNEL)
    if not tunnel:
        tunnel = api.call('POST', tunnels, {'name': TUNNEL, 'config_src': 'cloudflare'})
    if tunnel.get('config_src') != 'cloudflare' and not tunnel.get('remote_config'):
        raise ValueError('A remotely managed Cloudflare Tunnel is required for Friends VPN')
    tunnel_path = tunnels + '/' + tunnel['id']
    expected = [
        {'hostname': HOST, 'service': ORIGIN, 'originRequest': {
            'access': {'required': True, 'teamName': domain.split('.')[0], 'audTag': [app['aud']]}}},
        {'service': 'http_status:404'},
    ]
    try:
        saved = api.call('GET', tunnel_path + '/configurations') or {}
        current = (saved.get('config') or {}).get('ingress') or []
    except CloudflareError as error:
        if error.status != 404:
            raise
        current = []
    if any(rule.get('hostname') not in (None, HOST) or rule['service'] not in (ORIGIN, 'http_status:404')
           for rule in current):
        raise ValueError('The existing Cloudflare Tunnel contains a different route')
    if current != expected:
        api.call('PUT', tunnel_path + '/configurations', {'config': {'ingress': expected}})
    tunnel_token = api.call('GET', tunnel_path + '/token')

    records = '/zones/' + zone['id'] + '/dns_records'
    dns = one(api.list(records, name=HOST), HOST)
    target = tunnel['id'] + '.cfargotunnel.com'
    if not dns:
        api.call('POST', records, {'name': HOST, 'type': 'CNAME', 'content': target, 'proxied': True, 'ttl': 1})
    elif dns.get('content') != target or dns.get('type') != 'CNAME' or not dns.get('proxied'):
        raise ValueError(f'The existing DNS record for {HOST} points to a different service')

    return {'account_id': account_id, 'application_id': app['id'], 'policy_id': policy['id'],
            'issuer': 'https://' + domain, 'audience': app['aud']}, tunnel_token
