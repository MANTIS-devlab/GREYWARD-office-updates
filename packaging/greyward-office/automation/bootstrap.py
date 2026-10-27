#!/usr/bin/env python3
"""Export the small public distribution component; never publish or copy Git."""
import argparse
import datetime as dt
import hashlib
import json
from pathlib import Path
import shutil
import sys

from candidate import validate_sources
from model import UTC, new_state, timestamp
from seal import recipe_files


def export(repo, output):
    validate_sources(repo)
    output.mkdir(parents=True, exist_ok=False)
    files = recipe_files(repo)
    for source in files:
        target = output / source.relative_to(repo)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(source.read_bytes().replace(b'\r\n', b'\n'))
    for name in ('LICENSE', '.gitleaks.toml'):
        if (repo / name).is_file(): (output / name).write_bytes((repo / name).read_bytes().replace(b'\r\n', b'\n'))
    (output / 'tests/__init__.py').write_text('')
    workflows = output / '.github/workflows'; workflows.mkdir(parents=True)
    for original, name in [('workflow.yml', 'office.yml'), ('office-app.yml', 'office-app.yml'), ('validation.yml', 'validation.yml')]:
        shutil.copyfile(repo / 'packaging/greyward-office/automation' / original, workflows / name)
    (output / 'state').mkdir()
    (output / 'state/office.json').write_text(json.dumps(new_state(timestamp(dt.datetime.now(UTC))), indent=2) + '\n', newline='\n')
    (output / 'README.md').write_text(
        '# GREYWARD Office updates\n\n'
        'Public distribution component for GREYWARD Writer, Sheets, PDF and Slides.\n'
        'These customized applications are based on the work of the ArtCraft team:\n'
        '[WordCraft](https://github.com/storytold/wordcraft), '
        '[GridCraft](https://github.com/storytold/gridcraft), '
        '[PdfCraft](https://github.com/storytold/pdfcraft), '
        '[DeckCraft](https://github.com/storytold/deckcraft).\n\n'
        'GREYWARD branding does not make these official ArtCraft builds. Original\n'
        'licenses, attribution and corresponding sources accompany each generation.\n\n'
        'The automation policy starts disabled. Manual pilot runs build and test only.\n'
        'The channel is not available until signed publication and native DNF readback\n'
        'have passed. See the workflow results and signed channel state for its actual status.\n\n'
        'Once commissioned: complete stable-version check every six days; preserve\n'
        'the current and two preceding complete four-app generations. An obsolete\n'
        'fourth generation can remain for 48 hours during safe expiry. Interrupted\n'
        'staging drafts also expire after 48 hours. No OS releases are managed here.\n\n'
        'Builds have no signing or publication credentials. A separate transfer job\n'
        'moves one application at a time into a private draft and removes the Actions\n'
        'artifact; signing is a different job. Native DNF verification and a pinned\n'
        'public key gate the channel. Failed compatibility checks keep the prior version.\n\n'
        'This repository is a reviewed component export of '
        '[GREYWARD](https://github.com/MANTIS-devlab/GREYWARD), not a development-machine image.\n'
        , newline='\n')
    (output / '.gitattributes').write_text('* text=auto eol=lf\n', newline='\n')
    (output / '.gitignore').write_text('__pycache__/\n*.pyc\ncandidate/\nincoming/\nresult/\n', newline='\n')
    inventory = [{'path': p.relative_to(output).as_posix(), 'sha256': hashlib.sha256(p.read_bytes()).hexdigest()}
                 for p in sorted(output.rglob('*')) if p.is_file()]
    (output / 'component-export.json').write_text(json.dumps({'schema': 'greyward.office-component-export/v1',
                                                            'files': inventory}, indent=2) + '\n', newline='\n')
    previous = sys.dont_write_bytecode
    try:
        sys.dont_write_bytecode = True
        validate_sources(output)
    finally:
        sys.dont_write_bytecode = previous
    return len(inventory)


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--repo', type=Path, required=True); p.add_argument('--output', type=Path, required=True)
    a = p.parse_args(); print(json.dumps({'public_component_files': export(a.repo.resolve(), a.output.resolve())}))
