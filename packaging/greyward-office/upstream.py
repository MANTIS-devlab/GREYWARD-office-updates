"""Read-only stable ArtCraft release discovery for the GREYWARD build pipeline.

Discovery is not installation authority. Only reviewed GREYWARD builds and the
signed DNF repository can supply installable updates to the operating system.
"""
import datetime as dt
import json
import re
import urllib.request

REPOSITORIES = frozenset(('wordcraft', 'gridcraft', 'pdfcraft', 'deckcraft'))
LIMIT = 2 * 1024 * 1024
TAG = re.compile(r'v(0|[1-9][0-9]{0,8})\.(0|[1-9][0-9]{0,8})\.(0|[1-9][0-9]{0,8})\Z')


def request_json(repository, suffix):
    if repository not in REPOSITORIES or not re.fullmatch(r'releases(?:\?per_page=20|/tags/v[0-9.]+)', suffix):
        raise ValueError('Unapproved ArtCraft release endpoint')
    url = f'https://api.github.com/repos/storytold/{repository}/{suffix}'
    request = urllib.request.Request(url, headers={
        'Accept': 'application/vnd.github+json',
        'User-Agent': 'GREYWARD-Office-release-check',
        'X-GitHub-Api-Version': '2022-11-28',
    })
    with urllib.request.urlopen(request, timeout=20) as response:
        if response.geturl() != url:
            raise ValueError('Unexpected release API redirect')
        data = response.read(LIMIT + 1)
    if len(data) > LIMIT:
        raise ValueError('Release metadata exceeds the bounded response limit')
    return json.loads(data)


def stable_release(value, repository, now=None):
    if repository not in REPOSITORIES or not isinstance(value, dict):
        raise ValueError('Invalid ArtCraft release metadata')
    if value.get('draft') is not False or value.get('prerelease') is not False:
        raise ValueError('Only published stable releases are eligible')
    match = TAG.fullmatch(str(value.get('tag_name', '')))
    if match is None:
        raise ValueError('Stable semantic release tag required')
    try:
        published = dt.datetime.fromisoformat(value['published_at'].replace('Z', '+00:00'))
    except (KeyError, TypeError, ValueError, AttributeError) as error:
        raise ValueError('Release publication time is unavailable') from error
    observed = now or dt.datetime.now(dt.timezone.utc)
    if published.tzinfo is None or published > observed + dt.timedelta(minutes=5):
        raise ValueError('Release publication time is invalid or in the future')
    tag = value['tag_name']
    expected = f'https://github.com/storytold/{repository}/releases/tag/{tag}'
    if value.get('html_url') != expected:
        raise ValueError('Release does not belong to the expected official repository')
    return {'tag': tag, 'version': tag[1:], 'published_at': published.isoformat(), 'url': expected}


def latest_release(repository, fetch=request_json, now=None):
    values = fetch(repository, 'releases?per_page=20')
    if not isinstance(values, list) or len(values) > 20:
        raise ValueError('Invalid release listing')
    releases = []
    for value in values:
        if not isinstance(value, dict):
            raise ValueError('Invalid entry in the official release listing')
        # Preview and non-app tags are legitimate. A malformed published stable
        # entry must fail the check, not hide a potentially newer version.
        if value.get('draft') is True or value.get('prerelease') is True:
            continue
        if not TAG.fullmatch(str(value.get('tag_name', ''))):
            continue
        releases.append(stable_release(value, repository, now))
    if not releases:
        raise ValueError('No verified published stable release in the bounded listing')
    return max(releases, key=lambda item: tuple(map(int, item['version'].split('.'))))


def discover(manifest, fetch=request_json, now=None):
    result = {'schema': 'greyward.office-upstream-check/v1', 'applications': [], 'available': 0, 'unavailable': 0}
    for app in manifest['applications']:
        item = {'application': app['slug'], 'name': app['name'], 'source_version': app['version']}
        try:
            release = latest_release(app['upstream'], fetch, now)
            latest = tuple(map(int, release['version'].split('.')))
            pinned = tuple(map(int, app['version'].split('.')))
            if latest < pinned:
                raise ValueError('Official release listing is older than the reviewed source pin')
            newer = latest > pinned
            item.update(release, state='UPDATE_AVAILABLE' if newer else 'CURRENT')
            result['available'] += int(newer)
        except (OSError, ValueError) as error:
            item.update(state='UNAVAILABLE', reason=str(error)[:240])
            result['unavailable'] += 1
        result['applications'].append(item)
    return result
