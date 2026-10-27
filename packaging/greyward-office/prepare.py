#!/usr/bin/env python3
"""Verify immutable Office inputs and apply exact, reviewed adaptations."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tarfile
import tomllib
import urllib.request

from artwork import document_mime


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def verified_file(cache, item, filename):
    path = cache / filename
    if path.name != filename or '\\' in filename:
        raise ValueError('Input must be a plain filename')
    if not path.exists():
        temporary = path.with_suffix(path.suffix + '.part')
        with urllib.request.urlopen(item['url'], timeout=120) as remote, temporary.open('wb') as out:
            shutil.copyfileobj(remote, out)
        if digest(temporary) != item['sha256']:
            temporary.unlink()
            raise ValueError('Downloaded input digest mismatch: ' + filename)
        temporary.rename(path)
    if digest(path) != item['sha256']:
        raise ValueError('Input digest mismatch: ' + filename)
    return path


def extract(archive, destination, root_name):
    with tarfile.open(archive) as archive_file:
        for member in archive_file.getmembers():
            parts = Path(member.name).parts
            if not parts or parts[0] != root_name or member.issym() or member.islnk():
                raise ValueError('Unexpected archive member: ' + member.name)
        archive_file.extractall(destination, filter='data')
    return destination / root_name


def prepare(repo, cache, output, slug):
    manifest = json.loads((repo / 'packaging/greyward-office/source.json').read_text())
    app = next(a for a in manifest['applications'] if a['slug'] == slug)
    for name, expected in manifest['integrationSources'].items():
        if digest(repo / name) != expected:
            raise ValueError('Unreviewed integration source: ' + name)
    if output.exists():
        raise ValueError('Use a fresh output directory')
    cache.mkdir(parents=True, exist_ok=True)
    output.mkdir(parents=True)
    source = verified_file(cache, app, app['archive'])
    tree = extract(source, output, app['upstream'] + '-' + app['commit'])
    if digest(tree / 'Cargo.lock') != app['cargoLockSha256']:
        raise ValueError('Cargo.lock drift')
    if digest(tree / 'NOTICE') != app['noticeSha256']:
        raise ValueError('Upstream notice drift')
    for name, expected in app['adaptedInputs'].items():
        if digest(tree / name) != expected:
            raise ValueError('Adaptation input drift: ' + name)
    patch = repo / 'packaging/greyward-office' / app['patch']
    if digest(patch) != app['patchSha256']:
        raise ValueError('Unreviewed adaptation')
    subprocess.run(['git', 'apply', '--check', str(patch.resolve())], cwd=tree, check=True)
    subprocess.run(['git', 'apply', str(patch.resolve())], cwd=tree, check=True)
    if app.get('mimeSource') and digest(tree / app['mimeSource']) != app['mimeSha256']:
        raise ValueError('MIME definition drift')
    fonts = next(f for f in manifest['fonts'] if f['commit'] == app['fontsCommit'])
    font_archive = verified_file(cache, fonts, fonts['file'])
    font_tree = extract(font_archive, tree, 'craft-fonts-' + fonts['commit'])
    font_tree.rename(tree / 'greyward-fonts')
    if app.get('runtimeResources'):
        attribution = tree / 'ATTRIBUTION.toml'
        if digest(attribution) != app['resourceManifestSha256']:
            raise ValueError('Upstream runtime resource manifest drift')
        declared = [f for f in tomllib.loads(attribution.read_text())['fetched'] if f['kind'] == 'model']
        if declared != app['runtimeResources']:
            raise ValueError('Runtime resource selection differs from upstream')
        resources = tree / 'greyward-models'
        resources.mkdir()
        for item in declared:
            shutil.copyfile(verified_file(cache, item, item['file']), resources / item['file'])
            license_name = item['file'] + '.LICENCE.txt'
            license_source = repo / 'packaging/greyward-office/licenses' / (item['licence'] + '.txt')
            if digest(license_source) != item['licence_sha256']:
                raise ValueError('Bundled runtime resource license differs from upstream pin')
            shutil.copyfile(license_source, resources / license_name)
        (resources / 'ATTRIBUTION.json').write_text(json.dumps(declared, indent=2) + '\n')
    icon = repo / 'branding/source' / ('greyward-' + slug + '.svg')
    if digest(icon) != app['iconSha256']:
        raise ValueError('Unreviewed icon')
    assets = tree / 'greyward-branding'
    assets.mkdir()
    shutil.copyfile(icon, assets / 'application.svg')
    document_icon = repo / 'branding/source' / ('greyward-' + slug + '-document.svg')
    if digest(document_icon) != app['documentIconSha256']:
        raise ValueError('Unreviewed document icon')
    shutil.copyfile(document_icon, assets / 'document.svg')
    upstream_mime = (tree / app['mimeSource']).read_bytes() if app.get('mimeSource') else None
    if upstream_mime is not None:
        # Retain the original, hash-locked detection rules for payload validation.
        (assets / 'upstream-mime.xml').write_bytes(upstream_mime)
    (assets / 'document-mime.xml').write_bytes(document_mime(app, upstream_mime))
    for name in ('greyward-symbol.svg', 'greyward-wordmark.svg'):
        original = repo / 'branding/source' / name
        expected = manifest['brandSources'][name]
        if digest(original) != expected:
            raise ValueError('Brand source drift: ' + name)
        shutil.copyfile(original, assets / name)
    for size in (16, 24, 32, 48, 64, 128, 256, 512):
        subprocess.run(['rsvg-convert', '-w', str(size), '-h', str(size), '-o',
                        str(assets / f'{size}.png'), str(icon)], check=True)
    embedded = tree / 'assets/app-icon/hicolor/256x256/apps' / ('ai.storyteller.' + app['upstream'] + '.png')
    shutil.copyfile(assets / '256.png', embedded)
    receipt = {'schema': 'greyward.office-build/v1', 'application': app,
               'sourceManifestSha256': digest(repo / 'packaging/greyward-office/source.json'),
               'fonts': fonts, 'toolchain': manifest['toolchain'],
               'brandSources': manifest['brandSources'],
               'integrationSources': manifest['integrationSources']}
    (tree / 'greyward-provenance.json').write_text(json.dumps(receipt, indent=2) + '\n')
    return tree, app, manifest


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', type=Path, required=True)
    parser.add_argument('--cache', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('application', choices=('writer', 'sheets', 'pdf', 'slides'))
    args = parser.parse_args()
    print(prepare(args.repo.resolve(), args.cache.resolve(), args.output.resolve(), args.application)[0])
