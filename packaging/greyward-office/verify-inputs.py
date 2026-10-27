#!/usr/bin/env python3
"""Refuse incomplete or stale Office RPMs before production dependency closure."""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import stat
import subprocess

# Load only the verifier's trusted sibling, never code from an RPM/build output.
_spec = importlib.util.spec_from_file_location('greyward_office_artwork', Path(__file__).with_name('artwork.py'))
artwork = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(artwork)


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def validate_sources(repo):
    manifest_path = repo / 'packaging/greyward-office/source.json'
    manifest = json.loads(manifest_path.read_text())
    if (manifest.get('schema') != 'greyward.office-sources/v1' or manifest.get('candidateOnly')
            or not isinstance(manifest.get('rpmRelease'), int) or manifest['rpmRelease'] < 1):
        raise ValueError('Office manifest is not a promoted build lock')
    apps = manifest['applications']
    if {a['slug'] for a in apps} != {'writer', 'sheets', 'pdf', 'slides'} or len(apps) != 4:
        raise ValueError('Exactly four Office applications are required')
    for app in apps:
        name = {'writer': 'Writer', 'sheets': 'Sheets', 'pdf': 'PDF', 'slides': 'Slides'}[app['slug']]
        if (app['name'], app['package'], app['appId']) != (name, 'greyward-' + app['slug'], 'systems.mantis.greyward.' + name):
            raise ValueError('Office identity drift: ' + app['slug'])
        if not re.fullmatch(r'[0-9a-f]{40}', app['commit']):
            raise ValueError('Office source must use an immutable commit')
        paths = {'packaging/greyward-office/' + app['patch']: app['patchSha256'],
                 'branding/source/greyward-' + app['slug'] + '.svg': app['iconSha256'],
                 'branding/source/greyward-' + app['slug'] + '-document.svg': app['documentIconSha256']}
        if app['iconSha256'] == app['documentIconSha256']:
            raise ValueError('Application and document artwork must be distinct')
        for path, expected in paths.items():
            if digest(repo / path) != expected:
                raise ValueError('Office source drift: ' + path)
    for path, expected in manifest['integrationSources'].items():
        if digest(repo / path) != expected:
            raise ValueError('Office integration drift: ' + path)
    for path, expected in manifest['brandSources'].items():
        if digest(repo / 'branding/source' / path) != expected:
            raise ValueError('Office branding drift: ' + path)
    return manifest, digest(manifest_path)


def _read_members(rpm, members):
    source = subprocess.Popen(['rpm2cpio', str(rpm)], stdout=subprocess.PIPE)
    result = subprocess.run(['cpio', '-i', '--quiet', '--to-stdout', *members],
                            stdin=source.stdout, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    source.stdout.close()
    status = source.wait()
    if status or result.returncode:
        raise ValueError('Cannot inspect Office RPM payload')
    return result.stdout


def read_payload(rpm, path):
    data = _read_members(rpm, ['./' + path])
    if data:
        return data
    # RPM coalesces identical SVGs into newc hard links. Only one member carries
    # data; selecting its alias alone can return empty output. Select that exact
    # inode/device group together, then validate its bytes against the receipt.
    query = '[%{FILENAMES}|%{FILEINODES}|%{FILEDEVICES}\n]'
    rows = [line.split('|') for line in subprocess.check_output(
        ['rpm', '-qp', '--qf', query, str(rpm)], text=True).splitlines()]
    group = next((row[1:] for row in rows if row[0].lstrip('/') == path), None)
    members = ['./' + row[0].lstrip('/') for row in rows if row[1:] == group]
    if group and len(members) > 1:
        data = _read_members(rpm, members)
    if not data:
        raise ValueError('Office RPM payload missing: ' + path)
    return data


def verify_header(rpm, app):
    if subprocess.check_output(['rpm', '-qp', '--scripts', str(rpm)], text=True).strip():
        raise ValueError('Office RPM must not install privileged scriptlets: ' + app['package'])
    for row in subprocess.check_output(['rpm', '-qp', '--filecaps', str(rpm)], text=True).splitlines():
        fields = row.split('\t', 1)
        if len(fields) != 2 or fields[1].strip():
            raise ValueError('Office RPM must not grant file capabilities: ' + app['package'])
    exact = {'usr/bin/greyward-' + app['slug'], 'usr/libexec/greyward-office/' + app['upstream'],
             'usr/share/applications/' + app['appId'] + '.desktop',
             'usr/share/mime/packages/' + app['appId'] + '.xml',
             'usr/share/icons/hicolor/scalable/mimetypes/' + artwork.document_icon_name(app) + '.svg',
             'usr/share/icons/Greyward/scalable/mimetypes/' + artwork.document_icon_name(app) + '.svg',
             'usr/share/icons/hicolor/scalable/apps/' + app['appId'] + '.svg'}
    exact.update('usr/share/icons/hicolor/' + str(size) + 'x' + str(size) + '/apps/' + app['appId'] + '.png'
                 for size in (16, 24, 32, 48, 64, 128, 256, 512))
    trees = ('usr/share/licenses/' + app['package'], 'usr/share/doc/' + app['package'],
             'usr/share/greyward/office/' + app['slug'])
    query = '[%{FILENAMES}|%{FILEMODES:octal}|%{FILEUSERNAME}|%{FILEGROUPNAME}\n]'
    for row in subprocess.check_output(['rpm', '-qp', '--qf', query, str(rpm)], text=True).splitlines():
        name, mode, user, group = row.split('|')
        name, mode = name.lstrip('/'), int(mode, 8)
        if (name not in exact and not any(name == tree or name.startswith(tree + '/') for tree in trees)):
            raise ValueError('Unexpected Office RPM install path: ' + name)
        if (user, group) != ('root', 'root') or mode & 0o7022 or not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
            raise ValueError('Unsafe Office RPM ownership or mode: ' + name)


def verify_rpms(repo, rpms):
    manifest, sha = validate_sources(repo)
    expected = {a['package']: a for a in manifest['applications']}
    seen = set()
    for rpm in rpms:
        identity = subprocess.check_output(['rpm', '-qp', '--qf', '%{NAME}|%{VERSION}|%{RELEASE}|%{ARCH}', str(rpm)], text=True).strip().split('|')
        name, version, release, arch = identity
        if name not in expected or name in seen:
            raise ValueError('Unexpected/duplicate Office RPM: ' + name)
        app = expected[name]
        verify_header(rpm, app)
        if (version, release, arch) != (app['version'], str(manifest['rpmRelease']) + '.fc44', 'x86_64'):
            raise ValueError('Office RPM revision mismatch: ' + name)
        receipt = json.loads(read_payload(rpm, 'usr/share/greyward/office/' + app['slug'] + '/provenance.json'))
        fonts = next(f for f in manifest['fonts'] if f['commit'] == app['fontsCommit'])
        if (receipt.get('schema') != 'greyward.office-build/v1'
                or receipt.get('application') != app or receipt.get('sourceManifestSha256') != sha
                or receipt.get('integrationSources') != manifest['integrationSources']
                or receipt.get('fonts') != fonts or receipt.get('brandSources') != manifest['brandSources']
                or receipt.get('toolchain') != manifest['toolchain'] or not receipt.get('vendoredInputs')):
            raise ValueError('Office provenance mismatch: ' + name)
        required = {'usr/bin/greyward-' + app['slug'], 'usr/libexec/greyward-office/' + app['upstream'],
                    'usr/share/applications/' + app['appId'] + '.desktop',
                    'usr/share/mime/packages/' + app['appId'] + '.xml',
                    'usr/share/icons/hicolor/scalable/mimetypes/' + artwork.document_icon_name(app) + '.svg',
                    'usr/share/icons/Greyward/scalable/mimetypes/' + artwork.document_icon_name(app) + '.svg',
                    'usr/share/icons/hicolor/scalable/apps/' + app['appId'] + '.svg'}
        required.update('usr/share/icons/hicolor/' + str(size) + 'x' + str(size) + '/apps/' + app['appId'] + '.png'
                        for size in (16, 24, 32, 48, 64, 128, 256, 512))
        for item in app.get('runtimeResources', []):
            required.update({'usr/share/greyward/office/' + app['slug'] + '/models/' + item['file'],
                             'usr/share/greyward/office/' + app['slug'] + '/models/' + item['file'] + '.LICENCE.txt'})
        if app.get('mimeSource'):
            required.add('usr/share/greyward/office/' + app['slug'] + '/upstream-mime.xml')
        payloads = receipt.get('payloadFiles', {})
        if not required.issubset(payloads):
            raise ValueError('Incomplete Office payload receipt: ' + name)
        for path, expected_sha in payloads.items():
            if Path(path).is_absolute() or '..' in Path(path).parts:
                raise ValueError('Unsafe Office receipt path')
            if hashlib.sha256(read_payload(rpm, path)).hexdigest() != expected_sha:
                raise ValueError('Office payload drift: ' + path)
        icon = 'usr/share/icons/hicolor/scalable/apps/' + app['appId'] + '.svg'
        if payloads[icon] != app['iconSha256']:
            raise ValueError('Office icon does not match source')
        document_icon = 'usr/share/icons/hicolor/scalable/mimetypes/' + artwork.document_icon_name(app) + '.svg'
        if payloads[document_icon] != app['documentIconSha256']:
            raise ValueError('Office document icon does not match source')
        theme_icon = 'usr/share/icons/Greyward/scalable/mimetypes/' + artwork.document_icon_name(app) + '.svg'
        if payloads[theme_icon] != app['documentIconSha256']:
            raise ValueError('Office document icon does not match source theme')
        notice = read_payload(rpm, 'usr/share/licenses/' + app['package'] + '/NOTICE')
        if hashlib.sha256(notice).hexdigest() != app['noticeSha256']:
            raise ValueError('Office upstream notice does not match source')
        upstream_mime = None
        if app.get('mimeSource'):
            upstream_mime = read_payload(rpm, 'usr/share/greyward/office/' + app['slug'] + '/upstream-mime.xml')
            if hashlib.sha256(upstream_mime).hexdigest() != app['mimeSha256']:
                raise ValueError('Office MIME definition does not match source')
        mime = read_payload(rpm, 'usr/share/mime/packages/' + app['appId'] + '.xml')
        if mime != artwork.document_mime(app, upstream_mime):
            raise ValueError('Office document MIME icon metadata does not match source')
        for item in app.get('runtimeResources', []):
            prefix = 'usr/share/greyward/office/' + app['slug'] + '/models/' + item['file']
            if payloads[prefix] != item['sha256'] or payloads[prefix + '.LICENCE.txt'] != item['licence_sha256']:
                raise ValueError('Office runtime resource does not match source')
        seen.add(name)
    if seen != set(expected):
        raise ValueError('Incomplete Office RPM set: ' + ', '.join(sorted(set(expected) - seen)))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', type=Path, required=True)
    parser.add_argument('--rpm', type=Path, action='append', default=[])
    parser.add_argument('--sources-only', action='store_true')
    parser.add_argument('--stage', type=Path)
    args = parser.parse_args()
    try:
        if args.stage:
            snapshot = args.stage / 'office-source'
            if digest(snapshot / 'packaging/greyward-office/source.json') != digest(args.repo / 'packaging/greyward-office/source.json'):
                raise ValueError('Staged Office source manifest differs from canonical source')
            validate_sources(snapshot)
            rpms = [p for app in validate_sources(args.repo)[0]['applications']
                    for p in (args.stage / 'rpms').glob(app['package'] + '-*.rpm')]
            verify_rpms(args.repo, rpms)
        elif args.sources_only:
            validate_sources(args.repo)
        else:
            verify_rpms(args.repo, args.rpm)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        parser.exit(1, 'OFFICE_INPUT_VALIDATION=FAIL: ' + str(error) + '\n')
    print('OFFICE_INPUT_VALIDATION=PASS')
