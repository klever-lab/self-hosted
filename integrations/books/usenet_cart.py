"""Keep cart entries until every completed book is safely in OVH."""

from collections import Counter
from pathlib import Path
import json
import re
import tempfile
import time
from urllib.parse import urlsplit
from xml.etree import ElementTree

import requests

from althub_api import AltHub, HOSTS
from book_storage import sha256
from import_books import put_book, read_secret, sab_jobs, sab_request, usenet_directory, usenet_files

JOBS = 'usenet/jobs/'
RELEASES = 'usenet/releases/'


def job_release(job):
    for field in ('filename', 'nzb_name', 'name'):
        match = re.match(r'^althub-([0-9a-f]{32})(?:\.nzb)?$', job.get(field) or '')
        if match:
            return match[1]
    # Older SABnzbd feed jobs carry the original link in their history.
    url = urlsplit(job.get('url') or '')
    if url.scheme == 'https' and url.hostname in HOSTS and not url.username and url.port in (None, 443):
        match = re.match(r'^/getnzb/([0-9a-fA-F]{32})(?:\.nzb)?(?:[&/]|$)', url.path)
        if match:
            return match[1].lower()
    return None


def save_receipt(storage, key, contents):
    with tempfile.TemporaryDirectory() as temporary:
        receipt = Path(temporary) / 'receipt.json'
        receipt.write_text(json.dumps(contents, sort_keys=True) + '\n')
        storage.put(key, receipt, sha256(receipt))


def publish_completed(storage, history, releases, *, stage):
    saved_jobs = set(storage.objects(JOBS))
    completed_count = 0
    incomplete_count = 0
    for job in history:
        if job['status'] != 'Completed' or job['category'] != 'books':
            continue
        job_id = job['nzo_id']
        if not re.fullmatch(r'[A-Za-z0-9_-]+', job_id):
            raise RuntimeError('Invalid SABnzbd job ID')
        job_key = JOBS + job_id + '.json'
        if job_key in saved_jobs:
            continue
        if not usenet_directory(job).exists():
            # A restarted pod may have lost a completed but unacknowledged
            # download. Its cart entry is still available to queue again.
            incomplete_count += 1
            continue
        digests = sorted({put_book(storage, file) for file in usenet_files(job)})
        if not digests:
            incomplete_count += 1
            continue
        identifier = job_release(job)
        if identifier and identifier not in releases:
            save_receipt(storage, RELEASES + identifier + '.json', {'sha256': digests})
            releases.add(identifier)
        save_receipt(storage, job_key, {'release_id': identifier, 'sha256': digests})
        completed_count += 1
    if completed_count:
        print(f'Verified {completed_count} completed Usenet job(s) in OVH.', flush=True)
    if incomplete_count:
        message = f'{incomplete_count} completed Usenet job(s) lack recoverable book files.'
        if stage:
            raise RuntimeError(message)
        print(message, flush=True)


def recoverable_grabs(queue, cart):
    """Identify old feed grabs that have no payload and remain in the cart."""
    def normalize(value):
        return re.sub(r'[^a-z0-9]', '', value.casefold().removesuffix('.nzb'))

    cart_titles = Counter(normalize(title) for title in cart.values())
    queue_titles = Counter(normalize(job['filename']) for job in queue)
    result = []
    for job in queue:
        title = normalize(job['filename'])
        if (job['status'] != 'Grabbing' or job['cat'] != 'books' or float(job['mb']) != 0
                or not re.fullmatch(r'SABnzbd_nzo_[A-Za-z0-9_-]+', job['nzo_id'])
                or not title or cart_titles[title] != 1 or queue_titles[title] != 1):
            raise RuntimeError('SABnzbd still has work that cannot be recreated from the cart')
        result.append(job['nzo_id'])
    return result


def poll(storage, *, stage=False):
    if stage:
        with requests.Session() as session:
            althub = AltHub(read_secret('althub-api-key'), session)
            cart = althub.cart_items(read_secret('althub-cart-rss-url'))
        recoverable_grabs(list(sab_jobs('queue', category=None)), cart)
    history = list(sab_jobs('history'))
    releases = {key.removeprefix(RELEASES).removesuffix('.json')
                for key in storage.objects(RELEASES)}
    publish_completed(storage, history, releases, stage=stage)
    if stage:
        with requests.Session() as session:
            althub = AltHub(read_secret('althub-api-key'), session)
            cart = althub.cart_items(read_secret('althub-cart-rss-url'))
        grabs = recoverable_grabs(list(sab_jobs('queue', category=None)), cart)
        if grabs:
            sab_request('queue', name='delete', value=','.join(grabs), del_files=0)
            print(f'Removed {len(grabs)} idle grabs whose releases are still in the cart.', flush=True)
        return

    queue = {job_release(job) for job in sab_jobs('queue')}
    # Give provider failures a cooldown. A lost queue after a pod replacement
    # can be reconstructed from the cart and the persisted OVH receipts.
    recent_failures = {job_release(job) for job in history
                       if job['status'] == 'Failed' and time.time() - int(job.get('completed') or 0) < 900}
    with requests.Session() as session:
        althub = AltHub(read_secret('althub-api-key'), session)
        cart = set(althub.cart_items(read_secret('althub-cart-rss-url')))
        for identifier in sorted(cart & releases):
            if not storage.head(RELEASES + identifier + '.json'):
                raise RuntimeError('A verified Usenet release receipt is missing')
            althub.cart('remove', identifier)
        for identifier in sorted(cart - releases - queue - recent_failures):
            name = 'althub-' + identifier
            nzb = ElementTree.tostring(althub.nzb(identifier), encoding='utf-8', xml_declaration=True)
            result = sab_request('addfile', files={'nzbfile': (name + '.nzb', nzb)}, nzbname=name, cat='books',
                                 pp=3)
            if result.get('status') is not True or not result.get('nzo_ids'):
                raise RuntimeError('SABnzbd did not confirm its queued Usenet release')
            print(f'Queued altHUB release {identifier}.', flush=True)
