#!/usr/bin/env python3
"""Prepare a candidate stable-version lock, refusing changed adaptation inputs.

Never modifies the active manifest or publishes anything. If upstream changed
an adapted file, manually rebase/review the patch first; no fuzzy substitution.
"""
import argparse
import json
from pathlib import Path
import re
import subprocess
import tempfile
import urllib.request

from prepare import extract, digest


def candidate(args):
    repo = args.repo.resolve()
    manifest = json.loads((repo / 'packaging/greyward-office/source.json').read_text())
    app = next(a for a in manifest['applications'] if a['slug'] == args.application)
    if not re.fullmatch(r'\d+\.\d+\.\d+', args.version):
        raise ValueError('Stable semantic version required')
    if tuple(map(int, args.version.split('.'))) <= tuple(map(int, app['version'].split('.'))):
        raise ValueError('Select a newer stable upstream version')
    tag = 'v' + args.version
    base = 'https://api.github.com/repos/storytold/' + app['upstream']
    with urllib.request.urlopen(base + '/releases/tags/' + tag, timeout=60) as response:
        release = json.load(response)
    if release.get('prerelease') or release.get('draft') or release.get('tag_name') != tag:
        raise ValueError('Only a published stable upstream release is accepted')
    refs = subprocess.check_output(['git', 'ls-remote', 'https://github.com/storytold/' + app['upstream'] + '.git',
                                   'refs/tags/' + tag, 'refs/tags/' + tag + '^{}'], text=True).splitlines()
    reverse = {ref: sha for line in refs for sha, ref in [line.split('\t')]}
    commit = reverse.get('refs/tags/' + tag + '^{}', reverse.get('refs/tags/' + tag))
    if not commit or not re.fullmatch(r'[0-9a-f]{40}', commit):
        raise ValueError('Could not resolve immutable upstream tag')
    url = 'https://codeload.github.com/storytold/' + app['upstream'] + '/tar.gz/' + commit
    if args.output.exists():
        raise ValueError('Refusing to replace a candidate manifest')
    with tempfile.TemporaryDirectory(prefix='greyward-office-candidate-') as temporary:
        directory = Path(temporary)
        archive = directory / 'source.tar.gz'
        with urllib.request.urlopen(url, timeout=120) as response, archive.open('wb') as out:
            import shutil
            shutil.copyfileobj(response, out)
        tree = extract(archive, directory, app['upstream'] + '-' + commit)
        for name, expected in app['adaptedInputs'].items():
            if digest(tree / name) != expected:
                raise ValueError('Manual adaptation review required: ' + name)
        subprocess.run(['git', 'apply', '--check', str(repo / 'packaging/greyward-office' / app['patch'])], cwd=tree, check=True)
        app.update(version=args.version, tag=tag, commit=commit, url=url,
                   archive=app['upstream'] + '-' + commit + '.tar.gz',
                   sha256=digest(archive), cargoLockSha256=digest(tree / 'Cargo.lock'))
        # Font pins are part of upstream release inputs, not guessed from main.
        workflow = (tree / '.github/workflows/release.yml').read_text()
        if app['fontsCommit'] not in workflow:
            raise ValueError('Upstream font resource changed: inspect and lock the new resource before promotion')
        if app.get('runtimeResources') and digest(tree / 'ATTRIBUTION.toml') != app['resourceManifestSha256']:
            raise ValueError('Upstream runtime resource manifest changed: review its pins and notices')
    # The release is a shared generation: never downgrade unchanged siblings.
    manifest['rpmRelease'] += 1
    manifest['candidateOnly'] = True
    args.output.write_text(json.dumps(manifest, indent=2) + '\n')
    print('Candidate lock prepared. Build, source/RPM checks and acceptance are required before promotion.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('application', choices=('writer', 'sheets', 'pdf', 'slides'))
    parser.add_argument('version')
    args = parser.parse_args()
    try:
        candidate(args)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        parser.exit(1, str(error) + '\n')
