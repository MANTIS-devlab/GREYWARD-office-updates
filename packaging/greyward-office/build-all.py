#!/usr/bin/env python3
"""Build the four locked applications sequentially in an isolated builder unit."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', type=Path, required=True)
    parser.add_argument('--cache', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--target', type=Path, required=True)
    args = parser.parse_args()
    if os.geteuid() == 0:
        parser.error('Use a non-root build account inside a bounded build unit')
    manifest = json.loads((args.repo / 'packaging/greyward-office/source.json').read_text())
    expected = manifest['toolchain']['version']
    for program in ('rustc', 'cargo'):
        actual = subprocess.check_output([program, '--version'], text=True).split()[1]
        if actual != expected:
            parser.error(f'{program} {expected} is required by the reviewed toolchain lock; found {actual}')
    args.output.mkdir(parents=True, exist_ok=True)
    lock = (args.output / '.lock').open('a')
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    result = {}
    for slug in ('writer', 'sheets', 'pdf', 'slides'):
        result[slug] = 'building'
        (args.output / 'progress.json').write_text(json.dumps(result, indent=2) + '\n')
        command = [sys.executable, str(args.repo / 'packaging/greyward-office/build.py'),
                   '--repo', str(args.repo), '--cache', str(args.cache), '--output', str(args.output / slug),
                   '--target', str(args.target), slug]
        subprocess.run(command, check=True)
        result[slug] = 'built; acceptance required'
    (args.output / 'progress.json').write_text(json.dumps(result, indent=2) + '\n')


if __name__ == '__main__':
    main()
