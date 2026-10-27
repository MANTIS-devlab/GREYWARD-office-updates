"""Verify a candidate against trusted recipes before any signing credential use."""
import gzip
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import re
import shutil
import tarfile

from model import SLUGS, SHA


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def validate_candidate(base, value):
    if set(base) != set(value) or type(value.get('rpmRelease')) is not int or value['rpmRelease'] < base['rpmRelease']:
        raise ValueError('Invalid candidate generation/schema')
    for key in base:
        if key not in ('applications', 'rpmRelease') and base[key] != value[key]:
            raise ValueError('Candidate changed trusted recipe: ' + key)
    if len(value['applications']) != 4:
        raise ValueError('Incomplete candidate family')
    mutable = {'version', 'tag', 'commit', 'url', 'archive', 'sha256', 'cargoLockSha256', 'adaptedInputs', 'fontsWorkflowSource'}
    for old, new in zip(base['applications'], value['applications']):
        if set(old) != set(new) or any(old[k] != new[k] for k in old if k not in mutable):
            raise ValueError('Candidate changed reviewed identity/patch/resources')
        if not re.fullmatch(r'(0|[1-9][0-9]{0,8})\.(0|[1-9][0-9]{0,8})\.(0|[1-9][0-9]{0,8})', new['version']):
            raise ValueError('Invalid stable version')
        if tuple(map(int, new['version'].split('.'))) < tuple(map(int, old['version'].split('.'))):
            raise ValueError('Upstream downgrade')
        if new['version'] == old['version'] and any(new.get(k) != old.get(k) for k in mutable):
            raise ValueError('Published immutable version inputs changed')
        commit = new['commit']
        if (not re.fullmatch(r'[a-f0-9]{40}', commit) or new['tag'] != 'v' + new['version']
                or new['url'] != f'https://codeload.github.com/storytold/{old["upstream"]}/tar.gz/{commit}'
                or new['archive'] != old['upstream'] + '-' + commit + '.tar.gz'
                or set(new['adaptedInputs']) != set(old['adaptedInputs'])
                or not all(SHA.fullmatch(s) for s in [new['sha256'], new['cargoLockSha256'], *new['adaptedInputs'].values()])):
            raise ValueError('Candidate provenance/path drift')
        if new['version'] != old['version'] and new.get('fontsWorkflowSource'):
            if new['fontsWorkflowSource'].get('commit') != commit or not SHA.fullmatch(new['fontsWorkflowSource'].get('sha256', '')):
                raise ValueError('Font workflow provenance must refer to the captured release commit')


def recipe_files(repo):
    """Explicit public component closure; never copy host output, Git or secrets."""
    paths = [p for p in (repo / 'packaging/greyward-office').rglob('*') if p.is_file()
             and '__pycache__' not in p.parts and p.suffix != '.pyc']
    paths += [repo / 'branding/source' / name for name in
              ['greyward-symbol.svg', 'greyward-wordmark.svg'] +
              ['greyward-' + slug + suffix + '.svg' for slug in SLUGS for suffix in ('', '-document')]]
    paths += [repo / name for name in ('tests/test_office_automation.py',
                                     'tests/test_office_upstream.py', 'tests/fixtures/office-roundtrip.py')]
    for p in paths:
        if p.is_symlink() or not p.is_file():
            raise ValueError('Missing/unsafe recipe source: ' + str(p))
    return sorted(paths)


def recipe_archive(repo, output):
    with output.open('xb') as stream, gzip.GzipFile(fileobj=stream, mode='wb', mtime=0, filename='') as gz:
        with tarfile.open(fileobj=gz, mode='w') as archive:
            for p in recipe_files(repo):
                data = p.read_bytes()
                info = tarfile.TarInfo(p.relative_to(repo).as_posix())
                info.size = len(data); info.mode = 0o644; info.mtime = 0
                archive.addfile(info, io.BytesIO(data))


def verify_family(repo, rpms):
    spec = importlib.util.spec_from_file_location('sealed_office_inputs', repo / 'packaging/greyward-office/verify-inputs.py')
    inputs = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(inputs)
    inputs.verify_rpms(repo, rpms)


def seal(repo, incoming, candidate, output):
    base = json.loads((repo / 'packaging/greyward-office/source.json').read_text())
    lock = json.loads(candidate.read_text())
    validate_candidate(base, lock)
    output.mkdir(parents=True, exist_ok=False)
    scratch = output / 'recipe'
    for source in recipe_files(repo):
        target = scratch / source.relative_to(repo); target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    shutil.copyfile(candidate, scratch / 'packaging/greyward-office/source.json')
    binary, files = [], []
    for app in lock['applications']:
        slug = app['slug']
        for suffix in ('x86_64.rpm', 'src.rpm'):
            name = f'{app["package"]}-{app["version"]}-{lock["rpmRelease"]}.fc44.{suffix}'
            matches = list(incoming.rglob(name))
            if len(matches) != 1 or matches[0].is_symlink():
                raise ValueError('Missing/duplicate family RPM: ' + name)
            target = output / name; shutil.copyfile(matches[0], target); files.append(target)
            if suffix == 'x86_64.rpm': binary.append(target)
        for kind in ('engine', 'gui'):
            name = slug + '-' + kind + '.json'
            matches = list(incoming.rglob(name))
            if len(matches) != 1 or matches[0].is_symlink():
                raise ValueError('Missing native acceptance: ' + name)
            evidence = json.loads(matches[0].read_text())
            if kind == 'engine':
                if set(evidence) != {slug} or not evidence[slug] or any(v is not True for v in evidence[slug].values()):
                    raise ValueError('Engine round trip did not pass')
            elif evidence.get('private_x11_startup') is not True or evidence.get('no_tcp_udp_listener') is not True:
                raise ValueError('GUI/listener acceptance did not pass')
            target = output / name; shutil.copyfile(matches[0], target); files.append(target)
    verify_family(scratch, binary)
    target = output / 'source.json'; shutil.copyfile(candidate, target); files.append(target)
    recipe_archive(scratch, output / 'recipe.tar.gz'); files.append(output / 'recipe.tar.gz')
    inventory = {'schema': 'greyward.office-release/v1', 'generation': lock['rpmRelease'],
                 'versions': {a['slug']: a['version'] for a in lock['applications']},
                 'files': {p.name: digest(p) for p in sorted(files)}}
    import os
    inventory['builder'] = {'repository': os.environ.get('GITHUB_REPOSITORY'),
                            'run': os.environ.get('GITHUB_RUN_ID'), 'recipeCommit': os.environ.get('GITHUB_SHA')}
    (output / 'inventory.json').write_text(json.dumps(inventory, indent=2) + '\n')
    return inventory
