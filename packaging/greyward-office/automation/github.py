"""Bounded GitHub transport, scoped to the reviewed Office distribution repo."""
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import urllib.error
import urllib.parse
import urllib.request

REPOSITORY = 'MANTIS-devlab/GREYWARD-office-updates'


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError('Credential-bearing GitHub request must not redirect')


class GitHub:
    def __init__(self, token=None):
        self.token = token or os.environ.get('GH_TOKEN')
        self.opener = urllib.request.build_opener(NoRedirect())

    def request(self, suffix, method='GET', value=None, binary=False, missing=False, upload=False):
        if not suffix.startswith('/') or '?' in suffix.split('?')[0] or '..' in suffix.split('/'):
            raise ValueError('Invalid GitHub endpoint')
        domain = 'uploads.github.com' if upload else 'api.github.com'
        url = f'https://{domain}/repos/{REPOSITORY}' + suffix
        headers = {'Accept': 'application/vnd.github+json', 'User-Agent': 'GREYWARD-Office-distribution',
                   'X-GitHub-Api-Version': '2022-11-28'}
        if self.token:
            headers['Authorization'] = 'Bearer ' + self.token
        if method != 'GET' and not self.token:
            raise ValueError('GitHub publication credential is missing')
        data = value if binary else None if value is None else json.dumps(value).encode()
        if data is not None:
            headers['Content-Type'] = 'application/octet-stream' if binary else 'application/json'
        try:
            with self.opener.open(urllib.request.Request(url, data=data, headers=headers, method=method), timeout=120) as response:
                body = response.read(8 * 1024 * 1024 + 1)
                if len(body) > 8 * 1024 * 1024:
                    raise ValueError('GitHub response exceeds limit')
                return json.loads(body) if body else None
        except urllib.error.HTTPError as error:
            if missing and error.code == 404:
                return None
            # Never include request headers or credentials in errors.
            raise RuntimeError(f'GitHub {method} {suffix.split("?")[0]} failed: HTTP {error.code}') from None

    def releases(self):
        result = []
        for page in range(1, 11):
            values = self.request('/releases?per_page=100&page=' + str(page))
            result.extend(values)
            if len(values) < 100:
                return result
        raise ValueError('Release inventory exceeds bounded pagination')

    def content(self, path, ref='main', missing=False):
        if not re.fullmatch(r'[A-Za-z0-9_.\-/]+', path) or '..' in path.split('/'):
            raise ValueError('Invalid distribution path')
        result = self.request('/contents/' + path + '?ref=' + urllib.parse.quote(ref, safe=''), missing=missing)
        if result is None:
            return None
        if result.get('encoding') != 'base64' or result.get('type') != 'file':
            raise ValueError('Expected a bounded regular GitHub file')
        return base64.b64decode(result['content'])

    def commit(self, files, message):
        """Atomic commit on main; a concurrent update causes non-fast-forward failure."""
        head = self.request('/git/ref/heads/main')['object']['sha']
        parent = self.request('/git/commits/' + head)
        entries = []
        for path, data in files.items():
            if (not re.fullmatch(r'(?:channel|state|snapshots|packaging/greyward-office)/[A-Za-z0-9_.\-/]+', path)
                    or '..' in path.split('/') or len(data) > 2 * 1024 * 1024):
                raise ValueError('Refuse an unscoped/oversized generated commit path')
            blob = self.request('/git/blobs', 'POST', {'content': base64.b64encode(data).decode(), 'encoding': 'base64'})
            entries.append({'path': path, 'mode': '100644', 'type': 'blob', 'sha': blob['sha']})
        tree = self.request('/git/trees', 'POST', {'base_tree': parent['tree']['sha'], 'tree': entries})
        commit = self.request('/git/commits', 'POST', {'message': message, 'tree': tree['sha'], 'parents': [head]})
        self.request('/git/refs/heads/main', 'PATCH', {'sha': commit['sha'], 'force': False})
        return commit['sha']

    def download(self, url, destination, expected=None):
        prefix = 'https://github.com/' + REPOSITORY + '/releases/download/'
        if not url.startswith(prefix) or '..' in urllib.parse.urlsplit(url).path.split('/'):
            raise ValueError('Refuse a download outside owned release assets')
        sha = hashlib.sha256()
        # Anonymous redirects carry no API token, including to the asset CDN.
        with urllib.request.urlopen(url, timeout=120) as response, destination.open('xb') as out:
            size = 0
            while chunk := response.read(1024 * 1024):
                size += len(chunk)
                if size >= 2 * 1024 ** 3:
                    raise ValueError('Release asset exceeds GitHub limit')
                sha.update(chunk)
                out.write(chunk)
        result = sha.hexdigest()
        if expected and result != expected:
            raise ValueError('Anonymous release readback digest mismatch')
        return result

    def staged_asset(self, asset, destination):
        """Authenticated API hop only; never forward its token to an asset CDN."""
        if type(asset.get('id')) is not int or not self.token:
            raise ValueError('Owned staged asset/credential required')
        class Stop(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs): return None
        req = urllib.request.Request(f'https://api.github.com/repos/{REPOSITORY}/releases/assets/{asset["id"]}',
            headers={'Accept': 'application/octet-stream', 'Authorization': 'Bearer ' + self.token,
                     'User-Agent': 'GREYWARD-Office-staged-transfer', 'X-GitHub-Api-Version': '2022-11-28'})
        try:
            response = urllib.request.build_opener(Stop()).open(req, timeout=120)
        except urllib.error.HTTPError as error:
            if error.code not in (301, 302, 303, 307, 308): raise RuntimeError('Staged asset API download failed') from None
            target = error.headers.get('Location', '')
            parsed = urllib.parse.urlsplit(target)
            if parsed.scheme != 'https' or parsed.hostname not in ('release-assets.githubusercontent.com', 'objects.githubusercontent.com'):
                raise ValueError('Unexpected staged asset CDN')
            response = urllib.request.urlopen(target, timeout=120)  # Deliberately anonymous.
        sha = hashlib.sha256(); size = 0
        with response, destination.open('xb') as out:
            while chunk := response.read(1024 * 1024):
                size += len(chunk)
                if size >= 2 * 1024 ** 3: raise ValueError('Oversized staged asset')
                sha.update(chunk); out.write(chunk)
        if size != asset['size'] or asset.get('digest') != 'sha256:' + sha.hexdigest():
            raise ValueError('Staged asset readback mismatch')
