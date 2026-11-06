#!/usr/bin/env python3
"""Prepare a complete immutable family; exact compatibility, never fuzzy rebases."""
import argparse
import copy
import difflib
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import tarfile
import tomllib
from pathlib import PurePosixPath

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from prepare import digest, extract, verified_file
from upstream import discover, request_json, stable_release
from model import exact_adaptations
from notice import review_notice
from resource_inputs import declared_fonts, declared_language, validate_record, validate_fonts


def validate_sources(repo):
    spec = importlib.util.spec_from_file_location('office_inputs', repo / 'packaging/greyward-office/verify-inputs.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.validate_sources(repo)[0]


def resolve(repository, tag):
    result = subprocess.check_output(['git', 'ls-remote', 'https://github.com/storytold/' + repository + '.git',
                                     'refs/tags/' + tag, 'refs/tags/' + tag + '^{}'], text=True, timeout=60)
    refs = {ref: sha for line in result.splitlines() for sha, ref in [line.split('\t')]}
    commit = refs.get('refs/tags/' + tag + '^{}', refs.get('refs/tags/' + tag))
    if not commit or not re.fullmatch(r'[a-f0-9]{40}', commit):
        raise ValueError('Official tag could not be resolved immutably')
    return commit


def pinned_download(cache, repository, commit):
    """Capture bytes only after checking the official release/tag relationship.

    New hashes describe a new pinned source; existing cached files must not be
    trusted as a source of those hashes. Always acquire the candidate anew.
    """
    import hashlib
    import urllib.request
    url = f'https://codeload.github.com/storytold/{repository}/tar.gz/{commit}'
    filename = f'{repository}-{commit}.tar.gz'
    path = cache / filename
    with tempfile.NamedTemporaryFile(dir=cache, delete=False) as out:
        temporary = Path(out.name)
        try:
            with urllib.request.urlopen(url, timeout=120) as response:
                if response.geturl() != url:
                    raise ValueError('Unexpected source archive redirect')
                size = 0
                while chunk := response.read(1024 * 1024):
                    size += len(chunk)
                    if size > 512 * 1024 * 1024:
                        raise ValueError('Source archive exceeds limit')
                    out.write(chunk)
            out.flush()
            sha = digest(temporary)
            if path.exists() and digest(path) != sha:
                raise ValueError('Same immutable source changed bytes')
        except BaseException:
            out.close()
            temporary.unlink(missing_ok=True)
            raise
    try:
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
    return {'url': url, 'archive': filename, 'sha256': sha}, path


def extract_release(archive, destination, root_name):
    total = 0
    with tarfile.open(archive) as bundle:
        for count, member in enumerate(bundle, 1):
            parts = PurePosixPath(member.name).parts
            total += member.size
            if (count > 100000 or total > 2 * 1024 ** 3 or not parts or parts[0] != root_name
                    or any(p in ('..', '.git') for p in parts) or '\\' in member.name
                    or not (member.isfile() or member.isdir())):
                raise ValueError('Unsafe/unbounded upstream source archive')
    destination.mkdir(parents=True, exist_ok=False)
    return extract(archive, destination, root_name)


def adapt(app, old_tree, tree, patch):
    if digest(old_tree / 'NOTICE') != app['noticeSha256']:
        raise ValueError('Reviewed upstream NOTICE does not match its source lock')
    review_notice((old_tree / 'NOTICE').read_bytes(), (tree / 'NOTICE').read_bytes())
    old_licenses = {p.name: digest(p) for p in old_tree.glob('LICENSE*') if p.is_file()}
    new_licenses = {p.name: digest(p) for p in tree.glob('LICENSE*') if p.is_file()}
    if not old_licenses or old_licenses != new_licenses:
        raise ValueError('Upstream license changed; review required')
    brand_license = Path('docs/brand/LICENSE-brand.txt')
    old_brand, new_brand = old_tree / brand_license, tree / brand_license
    if (old_brand.exists() != new_brand.exists()
            or old_brand.exists() and old_brand.read_bytes() != new_brand.read_bytes()):
        raise ValueError('Upstream trademark license changed; review required')
    workflow = (tree / '.github/workflows/release.yml').read_text()
    if app['fontsCommit'] not in workflow:
        raise ValueError('Font resource changed; review required')
    # mimeSha256 describes the patched installed XML, not pristine upstream XML.
    # Compare pristine versions here; prepare.py checks the patched digest later.
    if app.get('mimeSource') and (tree / app['mimeSource']).read_bytes() != (old_tree / app['mimeSource']).read_bytes():
        raise ValueError('MIME definition changed; review required')
    if app.get('runtimeResources'):
        declared = [item for item in tomllib.loads((tree / 'ATTRIBUTION.toml').read_text(encoding='utf-8'))['fetched']
                    if item['kind'] == 'model']
        if declared != app['runtimeResources']:
            raise ValueError('OCR model input changed; review required')
    original = {name: (tree / name).read_bytes() for name in app['adaptedInputs']}
    expected = exact_adaptations(patch.read_bytes(), original)
    hashes = {name: digest(tree / name) for name in original}
    git = ['git', '-c', 'core.autocrlf=false', '-c', 'core.eol=lf', 'apply']
    # Source scratch directories may live inside the engineering checkout.
    # Do not let git discover that parent repository and silently skip hunks
    # relative to its prefix. No .git directory is created in upstream sources.
    env = dict(os.environ, GIT_CEILING_DIRECTORIES=str(tree.resolve().parent))
    subprocess.run([*git, '--check', str(patch.resolve())], cwd=tree, env=env, check=True)
    subprocess.run([*git, str(patch.resolve())], cwd=tree, env=env, check=True)
    if any((tree / name).read_bytes() != expected[name] for name in original):
        raise ValueError('git apply differed from the unique exact transformations')
    return hashes


def refresh_data(app, manifest, tree, cache, scratch):
    """Follow exact data commits declared by the selected official app release.

    A font/data revision is not a GREYWARD patch incompatibility. Root licenses
    remain unchanged, font rows require checked OFL notices/hashes, and all data
    and notices are retained in the complete source RPM.
    """
    def capture(repository, commit, old_record):
        if old_record and old_record['commit'] == commit:
            return old_record
        data, archive = pinned_download(cache, repository, commit)
        record = {'commit': commit, 'file': data['archive'], 'url': data['url'], 'sha256': data['sha256']}
        validate_record(record, repository)
        target = scratch / repository
        resource_tree = extract_release(archive, target / 'new', repository + '-' + commit)
        if old_record:
            old_archive = verified_file(cache, old_record, old_record['file'])
            old_tree = extract_release(old_archive, target / 'old', repository + '-' + old_record['commit'])
            before = {p.name: digest(p) for p in old_tree.glob('LICENSE*') if p.is_file()}
            after = {p.name: digest(p) for p in resource_tree.glob('LICENSE*') if p.is_file()}
            if not before or before != after:
                raise ValueError('Resource root license changed; review required: ' + repository)
        else:
            raise ValueError('New resource family requires initial review: ' + repository)
        if repository == 'craft-fonts':
            validate_fonts(resource_tree)
        return record

    old_fonts = next(r for r in manifest['fonts'] if r['commit'] == app['fontsCommit'])
    fonts = capture('craft-fonts', declared_fonts(tree), old_fonts)
    if fonts not in manifest['fonts']:
        manifest['fonts'].append(fonts)
    app['fontsCommit'] = fonts['commit']
    language = declared_language(tree)
    if language:
        app['languageData'] = capture('craft-i18n', language, app.get('languageData'))
    elif app.get('languageData'):
        raise ValueError('Upstream removed its language-data declaration; review required')


def prepare_family(repo, cache, output, bootstrap=False, generation=None):
    manifest = validate_sources(repo)
    report = discover(manifest)
    if report['unavailable']:
        raise ValueError('Incomplete official release discovery: ' + json.dumps(report))
    output.mkdir(parents=True, exist_ok=False)
    (output / 'discovery.json').write_text(json.dumps(report, indent=2) + '\n')
    cache.mkdir(parents=True, exist_ok=True)
    result = copy.deepcopy(manifest)
    changed = []
    notice_updates = []
    for app in result['applications']:
        release = next(r for r in report['applications'] if r['application'] == app['slug'])
        # Check published tag immutability even when no version changed.
        value = stable_release(request_json(app['upstream'], 'releases/tags/' + release['tag']), app['upstream'])
        commit = resolve(app['upstream'], release['tag'])
        if value['version'] == app['version']:
            if commit != app['commit']:
                raise ValueError('Published upstream tag moved: ' + app['slug'])
            continue
        patch = repo / 'packaging/greyward-office' / app['patch']
        archive_data, archive = pinned_download(cache, app['upstream'], commit)
        with tempfile.TemporaryDirectory(prefix='office-compatible-') as scratch:
            scratch = Path(scratch)
            old_archive = verified_file(cache, app, app['archive'])
            old_tree = extract_release(old_archive, scratch / 'old', app['upstream'] + '-' + app['commit'])
            tree = extract_release(archive, scratch / 'new', app['upstream'] + '-' + commit)
            refresh_data(app, result, tree, cache, scratch)
            app['adaptedInputs'] = adapt(app, old_tree, tree, patch)
            notice = review_notice((old_tree / 'NOTICE').read_bytes(), (tree / 'NOTICE').read_bytes())
            app['noticeSha256'] = notice['newSha256']
            if notice['changed']:
                notice_updates.append(dict(application=app['slug'], **notice))
                changes = output / 'notice-changes'
                changes.mkdir(exist_ok=True)
                diff = difflib.unified_diff(
                    (old_tree / 'NOTICE').read_text(encoding='utf-8-sig').splitlines(True),
                    (tree / 'NOTICE').read_text(encoding='utf-8-sig').splitlines(True),
                    fromfile=app['slug'] + '/previous/NOTICE', tofile=app['slug'] + '/candidate/NOTICE')
                (changes / (app['slug'] + '.diff')).write_text(''.join(diff), encoding='utf-8')
            app['cargoLockSha256'] = digest(tree / 'Cargo.lock')
            if app.get('runtimeResources'):
                app['resourceManifestSha256'] = digest(tree / 'ATTRIBUTION.toml')
            if app.get('fontsWorkflowSource'):
                app['fontsWorkflowSource'] = {'commit': commit, 'sha256': digest(tree / '.github/workflows/release.yml')}
        app.update(archive_data, version=value['version'], tag=value['tag'], commit=commit)
        changed.append(app['slug'])
    if changed:
        result['rpmRelease'] += 1
    if generation is not None:
        if type(generation) is not int or generation < result['rpmRelease']:
            raise ValueError('Reserved generation must not downgrade the build lock')
        result['rpmRelease'] = generation
    # This is an isolated build lock, not an accepted source/main promotion.
    # Canonical RPM checks still verify this exact complete lock and its payload.
    result.pop('candidateOnly', None)
    used_fonts = {app['fontsCommit'] for app in result['applications']}
    result['fonts'] = [r for r in result['fonts'] if r['commit'] in used_fonts]
    (output / 'source.json').write_text(json.dumps(result, indent=2) + '\n')
    decision = {'build': bool(changed) or bootstrap, 'changed': changed,
                'generation': result['rpmRelease'], 'noticeUpdates': notice_updates}
    (output / 'decision.json').write_text(json.dumps(decision, indent=2) + '\n')
    return decision


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', type=Path, required=True)
    parser.add_argument('--cache', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--bootstrap', action='store_true')
    parser.add_argument('--generation', type=int)
    args = parser.parse_args()
    print(json.dumps(prepare_family(args.repo.resolve(), args.cache.resolve(), args.output.resolve(), args.bootstrap, args.generation)))
