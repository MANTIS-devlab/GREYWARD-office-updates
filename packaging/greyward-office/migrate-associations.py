#!/usr/bin/env python3
"""Replace retired Office handlers while preserving unrelated user choices.

Writes a separate reviewed output; the caller owns backup and atomic activation.
Run only after the four replacements have passed acceptance.
"""
import argparse
import configparser
import io
from pathlib import Path

RETIRED = {'com.collaboraoffice.Office.desktop', 'org.gnome.Papers.desktop'}


def parser():
    result = configparser.ConfigParser(interpolation=None, strict=False)
    result.optionxform = str
    return result


def migrate(existing, canonical):
    defaults, current = parser(), parser()
    defaults.read_string(canonical)
    current.read_string(existing)
    for section in current.sections():
        for mime, value in list(current[section].items()):
            handlers = [h for h in value.split(';') if h]
            if not RETIRED.intersection(handlers):
                continue
            replacement = defaults.get('Default Applications', mime, fallback='').split(';')
            # Removed Associations is a deny list; never insert the replacement there.
            migrated = []
            for handler in handlers:
                values = replacement if handler in RETIRED and section != 'Removed Associations' else [handler]
                if handler in RETIRED and section == 'Removed Associations':
                    values = []
                for new in values:
                    if new and new not in migrated:
                        migrated.append(new)
            if migrated:
                current[section][mime] = ';'.join(migrated) + ';'
            else:
                del current[section][mime]
    stream = io.StringIO()
    current.write(stream, space_around_delimiters=False)
    return stream.getvalue()


if __name__ == '__main__':
    arguments = argparse.ArgumentParser(description=__doc__)
    arguments.add_argument('--input', type=Path, required=True)
    arguments.add_argument('--canonical', type=Path, required=True)
    arguments.add_argument('--output', type=Path, required=True)
    args = arguments.parse_args()
    if args.output.exists():
        arguments.error('Use a new output file; preserve the original')
    args.output.write_text(migrate(args.input.read_text(), args.canonical.read_text()))
