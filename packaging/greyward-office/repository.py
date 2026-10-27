#!/usr/bin/env python3
"""Prepare a signed Office RPM repository; never publish or enable it.

The caller supplies an existing signing key by full fingerprint and a private
GNUPGHOME. A test key must never become the production distribution key.
"""
import argparse
import importlib.util
import os
from pathlib import Path
import re
import shutil
import subprocess


def create(args):
    spec = importlib.util.spec_from_file_location('office_inputs', Path(__file__).with_name('verify-inputs.py'))
    inputs = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(inputs)
    # Signing is a promotion boundary: reject partial or stale families first.
    inputs.verify_rpms(args.repo.resolve(), args.rpm)
    fingerprint = args.fingerprint.upper()
    if not re.fullmatch(r'[0-9A-F]{40}|[0-9A-F]{64}', fingerprint):
        raise ValueError('Use the complete signing-key fingerprint')
    env = dict(os.environ, GNUPGHOME=str(args.gnupg_home.resolve()))
    key = subprocess.check_output(['gpg', '--batch', '--with-colons', '--fingerprint', fingerprint], env=env, text=True)
    if not any(line.startswith('fpr:') and line.split(':')[9] == fingerprint for line in key.splitlines()):
        raise ValueError('Signing-key fingerprint mismatch')
    if args.output.exists():
        raise ValueError('Use a fresh repository output directory')
    args.output.mkdir(parents=True)
    for rpm in args.rpm:
        target = args.output / rpm.name
        if target.exists():
            raise ValueError('Duplicate RPM filename')
        shutil.copyfile(rpm, target)
        subprocess.run(['rpmsign', '--define', '_openpgp_sign_id ' + fingerprint,
                        '--addsign', str(target)], env=env, check=True)
    subprocess.run(['createrepo_c', str(args.output)], check=True)
    metadata = args.output / 'repodata/repomd.xml'
    subprocess.run(['gpg', '--batch', '--armor', '--local-user', fingerprint,
                    '--detach-sign', str(metadata)], env=env, check=True)
    public = subprocess.check_output(['gpg', '--batch', '--armor', '--export', fingerprint], env=env)
    (args.output / 'RPM-GPG-KEY-greyward-office').write_bytes(public)
    print('Prepared signed repository; no host trust or repository configuration changed.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', type=Path, required=True)
    parser.add_argument('--gnupg-home', type=Path, required=True)
    parser.add_argument('--fingerprint', required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--rpm', type=Path, action='append', required=True)
    args = parser.parse_args()
    try:
        create(args)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        parser.exit(1, str(error) + '\n')
