#!/usr/bin/env python3
"""Build one source RPM and binary RPM offline after locked Cargo vendoring.

Run unprivileged in a bounded Fedora 44 build unit. Never install build tools in
the product. Promotion and repository signing are separate operations.
"""
import argparse
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import tarfile

from prepare import prepare, digest


from artwork import MIMES


def build(args):
    if os.geteuid() == 0:
        raise ValueError('Office builds require an unprivileged isolated build account')
    repo, output = args.repo.resolve(), args.output.resolve()
    lock = json.loads((repo / 'packaging/greyward-office/source.json').read_text())
    fedora = subprocess.check_output(['rpm', '--eval', '%{fedora}'], text=True).strip()
    if fedora != str(lock['fedora']) or platform.machine() != lock['architecture']:
        raise ValueError('Office builds require the reviewed Fedora/architecture target')
    for program in ('rustc', 'cargo'):
        actual = subprocess.check_output([program, '--version'], text=True).split()[1]
        if actual != lock['toolchain']['version']:
            raise ValueError('Toolchain drift: ' + program + ' ' + actual)
    tree, app, manifest = prepare(repo, args.cache.resolve(), output / 'prepared', args.application)
    env = os.environ.copy()
    env['CARGO_BUILD_JOBS'] = '2'
    env['CARGO_PROFILE_RELEASE_DEBUG'] = '0'
    env['CARGO_PROFILE_RELEASE_LTO'] = 'false'
    env['CARGO_PROFILE_RELEASE_CODEGEN_UNITS'] = '16'
    # A separate shared target speeds sequential builds, but never enters the SRPM.
    env['CARGO_TARGET_DIR'] = str(args.target.resolve())
    config = tree / '.cargo/config.toml'
    # Preserve existing linker/target settings, append source redirection only.
    config.parent.mkdir(exist_ok=True)
    result = subprocess.run(['cargo', 'vendor', '--locked', '--versioned-dirs', 'greyward-vendor'],
                            cwd=tree, env=env, text=True, stdout=subprocess.PIPE, check=True)
    with config.open('a') as stream:
        stream.write('\n' + result.stdout)
    notices = tree / 'greyward-licenses'
    notices.mkdir()
    # Preserve bundled dependency/font notices alongside the application license.
    for folder in (tree / 'greyward-vendor', tree / 'greyward-fonts', tree / 'assets', tree / 'crates'):
        for source in folder.rglob('*'):
            name = source.name.lower()
            license_name = re.search(r'(^|[-_])(unlicense|license|licence|copying|copyright|notice|ofl)([._-]|$)', name)
            if source.is_file() and license_name and source.suffix.lower() not in {'.rs', '.c', '.h', '.cpp', '.py', '.js', '.toml', '.json'}:
                target = notices / source.relative_to(tree)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, target)
    metadata = subprocess.check_output(['cargo', 'metadata', '--locked', '--offline', '--format-version=1'],
                                        cwd=tree, env=env, text=True)
    packages = json.loads(metadata)['packages']
    (notices / 'dependencies.json').write_text(json.dumps([
        {k: p.get(k) for k in ('name', 'version', 'source', 'license', 'license_file', 'repository')}
        for p in packages], indent=2) + '\n')
    provenance = json.loads((tree / 'greyward-provenance.json').read_text())
    provenance['vendoredInputs'] = {p.relative_to(tree / 'greyward-vendor').as_posix(): digest(p)
                                  for p in sorted((tree / 'greyward-vendor').rglob('.cargo-checksum.json'))}
    (tree / 'greyward-provenance.json').write_text(json.dumps(provenance, indent=2) + '\n')
    for directory in ('BUILD', 'BUILDROOT', 'RPMS', 'SOURCES', 'SPECS', 'SRPMS', 'tmp'):
        (output / directory).mkdir(parents=True, exist_ok=True)
    source_name = app['package'] + '-' + app['version']
    archive = output / 'SOURCES' / (source_name + '.tar.gz')
    with tarfile.open(archive, 'w:gz') as bundle:
        bundle.add(tree, arcname=source_name)
    spec = (repo / 'packaging/greyward-office/office.spec.in').read_text()
    replacements = {**app, 'release': str(args.release or manifest['rpmRelease']),
                    'sourceName': source_name, 'mimes': ';'.join(MIMES[args.application]) + ';',
                    'rustVersion': manifest['toolchain']['version'], 'envPrefix': app['upstream'].upper(),
                    'license': '(MIT OR Apache-2.0) AND OFL-1.1' + (' AND CC-BY-SA-4.0' if args.application == 'pdf' else ''),
                    'fileArgument': '%U' if args.application == 'pdf' else '%F'}
    for key, value in replacements.items():
        if isinstance(value, str):
            spec = spec.replace('@' + key + '@', value)
    spec_path = output / 'SPECS' / (app['package'] + '.spec')
    spec_path.write_text(spec)
    subprocess.run(['rpmbuild', '-ba', '--define', '_topdir ' + str(output),
                    '--define', '_tmppath ' + str(output / 'tmp'),
                    '--define', 'cargo ' + shutil.which('cargo'),
                    '--define', '_build_id_links none', str(spec_path)], env=env, check=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', type=Path, required=True)
    parser.add_argument('--cache', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--target', type=Path, required=True)
    parser.add_argument('--release', type=int)
    parser.add_argument('application', choices=tuple(MIMES))
    build(parser.parse_args())
