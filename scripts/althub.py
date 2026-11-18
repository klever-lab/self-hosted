"""Search altHUB, inspect NZB filenames, and manage individual cart entries."""

import argparse
import json
from pathlib import Path
import sys

import requests

import manage

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'integrations/books'))
from althub_api import AltHub, AltHubError, CATEGORIES, integer, release_id


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
