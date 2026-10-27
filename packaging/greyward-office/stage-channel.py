#!/usr/bin/env python3
"""Stage the approved Office DNF channel for future installed systems only.

Does not contact the network, import keys, enable a host repository or publish.
Keep the channel separate from offline dependency resolution and app payloads.
"""
import argparse
import configparser
import hashlib
import json
from pathlib import Path
import re

KEY = 'RPM-GPG-KEY-greyward-office.gpg'
PACKAGES = 'greyward-writer greyward-sheets greyward-pdf greyward-slides'
MIRRORLIST = 'https://raw.githubusercontent.com/MANTIS-devlab/GREYWARD-office-updates/main/channel/f$releasever-$basearch/mirrorlist.txt'


def stage(repo, offline, require_published=False, verify=False):
    source = repo / 'packaging/greyward-office'
    policy = json.loads((source / 'channel.json').read_text(encoding='utf-8'))
    if (policy.get('schema') != 'greyward.office-channel/v1'
            or type(policy.get('published')) is not bool
            or not isinstance(policy.get('fingerprint'), str)
            or not re.fullmatch(r'[A-F0-9]{40}|[A-F0-9]{64}', policy.get('fingerprint', ''))):
        raise ValueError('Invalid Office channel approval/key policy')
    key = (source / 'keys' / KEY).read_bytes()
    if hashlib.sha256(key).hexdigest() != policy.get('keySha256'):
        raise ValueError('Office distribution key drift')
    template = (source / 'greyward-office.repo.in').read_text(encoding='utf-8')
    config = configparser.ConfigParser(interpolation=None)
    config.read_string(template)
    if config.sections() != ['greyward-office'] or dict(config['greyward-office']) != {
        'name': 'GREYWARD Office', 'mirrorlist': MIRRORLIST, 'enabled': '0',
        'gpgcheck': '1', 'repo_gpgcheck': '1', 'gpgkey': 'file:///etc/pki/rpm-gpg/' + KEY,
        'skip_if_unavailable': 'False', 'metadata_expire': '6h', 'includepkgs': PACKAGES,
    }:
        raise ValueError('Office channel template weakens the reviewed DNF contract')
    if not policy['published']:
        if require_published:
            raise ValueError('Office channel is unpublished/unapproved; refuse an image without Office updates')
        return False
    expected = template.replace('enabled=0', 'enabled=1').encode('utf-8')
    paths = [offline / 'keys' / KEY, offline / 'update-repos/greyward-office.repo']
    if verify:
        if paths[0].read_bytes() != key or paths[1].read_bytes() != expected:
            raise ValueError('Missing or stale installed Office update channel')
        return True
    # Preserve prior staging rather than silently replace another trust policy.
    for directory in ('keys', 'update-repos'):
        (offline / directory).mkdir(parents=True, exist_ok=True)
    if any(path.exists() or path.is_symlink() for path in paths):
        raise ValueError('Office channel is already staged')
    paths[0].write_bytes(key)
    paths[1].write_bytes(expected)
    return True


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', type=Path, required=True)
    parser.add_argument('--offline', type=Path, required=True)
    parser.add_argument('--require-published', action='store_true')
    parser.add_argument('--verify', action='store_true', help='Check an existing staged/extracted tree without modifying it')
    args = parser.parse_args()
    try:
        enabled = stage(args.repo, args.offline, args.require_published, args.verify)
        print('Office future-update channel staged.' if enabled else 'Office channel unpublished; no repository staged.')
    except (OSError, ValueError, configparser.Error) as error:
        parser.exit(1, str(error) + '\n')
