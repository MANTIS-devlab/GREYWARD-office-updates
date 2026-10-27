#!/usr/bin/env python3
"""Compare the reviewed Office source family with official stable releases.

Exit 0: current source pins; 2: newer stable version; 1: incomplete verification.
Never downloads executables, changes source pins, installs, signs or publishes.
"""
import argparse
import json
from pathlib import Path
from upstream import discover


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', type=Path, required=True)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    manifest = json.loads((args.repo / 'packaging/greyward-office/source.json').read_text(encoding='utf-8'))
    result = discover(manifest)
    text = json.dumps(result, indent=2) + '\n'
    if args.output:
        with args.output.open('x', encoding='utf-8') as output:
            output.write(text)
    print(text, end='')
    return 1 if result['unavailable'] else 2 if result['available'] else 0


if __name__ == '__main__':
    raise SystemExit(main())
