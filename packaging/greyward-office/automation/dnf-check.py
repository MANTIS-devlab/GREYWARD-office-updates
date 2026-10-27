#!/usr/bin/env python3
"""Real DNF install/upgrade and negative signatures in disposable RPM databases.

The foundation header is test-only; neither it nor negative keys leave scratch.
No application scriptlet or payload is installed on the host.
"""
import argparse
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile

PACKAGES = ['greyward-' + s for s in ('writer', 'sheets', 'pdf', 'slides')]


def run(argv, **kwargs):
    result = subprocess.run(list(map(str, argv)), capture_output=True, text=True, timeout=600, **kwargs)
    if result.returncode:
        raise ValueError('Native DNF fixture command failed: ' + str(argv[0]) + '\n' + (result.stdout + result.stderr)[-2500:])
    return result.stdout


def check(repository, home, fingerprint, output, previous=None, remote=None):
    if os.geteuid() != 0:
        raise ValueError('Disposable database fixture needs container root')
    output.mkdir(parents=True, exist_ok=False)
    host_before = sorted(run(['rpm', '-qa']).splitlines())
    db = Path(run(['rpm', '--eval', '%{_dbpath}']).strip())
    if not (db / 'rpmdb.sqlite').is_file():
        raise ValueError('Expected Fedora SQLite RPM database')
    # Satisfy only the product dependency absent from the disposable CI image.
    top = output / 'foundation'
    for name in ('SPECS', 'SOURCES', 'BUILD', 'BUILDROOT', 'RPMS', 'SRPMS'):
        (top / name).mkdir(parents=True)
    spec = top / 'SPECS/foundation.spec'
    spec.write_text('Name: office-ci-foundation\nVersion: 1\nRelease: 1\nSummary: Disposable test dependency\n'
                    'License: MIT\nBuildArch: noarch\nProvides: greyward-security-center\n'
                    '%description\nTest only. Never distribute.\n%files\n')
    run(['rpmbuild', '-bb', '--define', '_topdir ' + str(top), spec])
    foundation = next((top / 'RPMS').rglob('*.rpm'))
    config = output / 'dnf.conf'; config.write_text('[main]\ninstall_weak_deps=False\ngpgcheck=True\n')

    def transaction(label, location):
        root = output / label / 'root'; target = root / db.relative_to('/'); target.mkdir(parents=True)
        with sqlite3.connect((db / 'rpmdb.sqlite').as_uri() + '?mode=ro', uri=True) as source:
            with sqlite3.connect(target / 'rpmdb.sqlite') as destination: source.backup(destination)
        run(['rpm', '--root', root, '--import', repository / 'RPM-GPG-KEY-greyward-office'])
        run(['rpm', '--root', root, '-i', '--justdb', '--nodeps', foundation])
        if previous:
            old = list(previous.glob('*.x86_64.rpm'))
            if len(old) != 4: raise ValueError('Previous generation incomplete')
            run(['rpm', '--root', root, '-i', '--justdb', '--nodeps', '--noscripts', *old])
        # DNF may legitimately import repository certificates into this private
        # database. Compare installed package identities, not key rows/order.
        before = sorted(x for x in run(['rpm', '--root', root, '-qa']).splitlines() if not x.startswith('gpg-pubkey-'))
        repos = output / label / 'repos'; repos.mkdir()
        source_key = 'mirrorlist' if location.endswith('/mirrorlist.txt') else 'baseurl'
        (repos / 'office.repo').write_text('[office-ci]\nname=Office CI\n' + source_key + '=' + location +
            '\nenabled=1\ngpgcheck=1\nrepo_gpgcheck=1\ngpgkey=' + (repository / 'RPM-GPG-KEY-greyward-office').as_uri() +
            '\nmetadata_expire=0\nskip_if_unavailable=False\nincludepkgs=' + ' '.join(PACKAGES) + '\n')
        command = ['dnf5', '--installroot=' + str(root), '--releasever=44', '-y', '--config=' + str(config),
                   '--setopt=reposdir=' + str(repos), '--setopt=cachedir=' + str(output / label / 'cache'),
                   '--setopt=system_cachedir=' + str(output / label / 'system-cache'),
                   '--setopt=logdir=' + str(output / label / 'logs'), '--setopt=tsflags=test',
                   'upgrade' if previous else 'install', *PACKAGES]
        result = subprocess.run(command, capture_output=True, text=True, timeout=600)
        (output / (label + '.log')).write_text(result.stdout + result.stderr)
        after = sorted(x for x in run(['rpm', '--root', root, '-qa']).splitlines() if not x.startswith('gpg-pubkey-'))
        if after != before:
            raise ValueError('Disposable headers changed during test transaction')
        return result

    positive = transaction('valid', remote or repository.as_uri())
    if positive.returncode: raise ValueError('Signed DNF transaction failed: ' + (positive.stdout + positive.stderr)[-2000:])
    for rpm in repository.glob('*.x86_64.rpm'):
        name, version = run(['rpm', '-qp', '--qf', '%{NAME}|%{VERSION}-%{RELEASE}', rpm]).strip().split('|')
        if name not in positive.stdout or version not in positive.stdout:
            raise ValueError('DNF did not resolve every expected current package; not a verified update')
    negative = output / 'negative'; negative.mkdir()
    for rpm in repository.glob('*.x86_64.rpm'): shutil.copyfile(rpm, negative / rpm.name)
    negative_home = output / 'negative-home'; negative_home.mkdir(mode=0o700)
    bad_env = dict(os.environ, GNUPGHOME=str(negative_home))
    run(['gpg', '--batch', '--passphrase', '', '--quick-generate-key', 'Disposable Office negative test', 'ed25519', 'sign', '1d'], env=bad_env)
    data = run(['gpg', '--batch', '--with-colons', '--list-secret-keys'], env=bad_env)
    bad_fpr = next(line.split(':')[9] for line in data.splitlines() if line.startswith('fpr:'))
    bad_rpm = next(negative.glob('greyward-writer-*.rpm'))
    run(['rpmsign', '--define', '_openpgp_sign_id ' + bad_fpr, '--resign', bad_rpm], env=bad_env)
    run(['createrepo_c', negative])
    run(['gpg', '--batch', '--armor', '--local-user', fingerprint, '--detach-sign', negative / 'repodata/repomd.xml'],
        env=dict(os.environ, GNUPGHOME=str(home)))
    invalid = transaction('untrusted', negative.as_uri())
    if invalid.returncode == 0 or not any(word in (invalid.stdout + invalid.stderr).lower() for word in ('signature', 'public key', 'gpg', 'pgp')):
        raise ValueError('Untrusted package was not rejected for its signature')
    tampered = output / 'tampered-metadata'
    shutil.copytree(repository / 'repodata', tampered / 'repodata')
    for rpm in repository.glob('*.x86_64.rpm'): shutil.copyfile(rpm, tampered / rpm.name)
    (tampered / 'repodata/repomd.xml.asc').write_bytes(b'invalid detached signature\n')
    bad_metadata = transaction('invalid-metadata', tampered.as_uri())
    if bad_metadata.returncode == 0 or not any(word in (bad_metadata.stdout + bad_metadata.stderr).lower()
                                             for word in ('signature', 'public key', 'gpg', 'pgp')):
        raise ValueError('Invalid repository metadata signature was not explicitly rejected')
    if sorted(run(['rpm', '-qa']).splitlines()) != host_before:
        raise ValueError('Host package identities changed during disposable-root checks')
    evidence = {'real_dnf_transaction_test': True, 'operation': 'upgrade' if previous else 'initial-install',
                'untrusted_rpm_rejected': True, 'untrusted_metadata_rejected': True,
                'host_unchanged': True, 'reference_reboot_test': False}
    (output / 'results.json').write_text(json.dumps(evidence, indent=2) + '\n')
    return evidence


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--repository', type=Path, required=True); p.add_argument('--home', type=Path, required=True)
    p.add_argument('--fingerprint', required=True); p.add_argument('--output', type=Path, required=True)
    p.add_argument('--previous', type=Path); p.add_argument('--remote')
    a = p.parse_args(); print(json.dumps(check(a.repository.resolve(), a.home.resolve(), a.fingerprint, a.output.resolve(), a.previous, a.remote)))
