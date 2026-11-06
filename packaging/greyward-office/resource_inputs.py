"""Pinned, upstream-declared data inputs; never execute a resource repository."""
import hashlib
from pathlib import Path, PurePosixPath
import re

SHA = re.compile(r'[a-f0-9]{64}')
COMMIT = re.compile(r'[a-f0-9]{40}')


def validate_record(value, repository):
    if set(value) != {'commit', 'file', 'url', 'sha256'}:
        raise ValueError('Unexpected resource record')
    commit = value['commit']
    if (not COMMIT.fullmatch(commit) or not SHA.fullmatch(value['sha256'])
            or value['file'] != repository + '-' + commit + '.tar.gz'
            or value['url'] != 'https://codeload.github.com/storytold/' + repository + '/tar.gz/' + commit):
        raise ValueError('Resource must be pinned to its official immutable source')


def declared_fonts(tree):
    workflow = (tree / '.github/workflows/release.yml').read_text(encoding='utf-8')
    refs = re.findall(r'^\s+craft-fonts-ref:\s*([a-f0-9]{40})\s*$', workflow, re.MULTILINE)
    if len(refs) != 1:
        raise ValueError('Expected one immutable craft-fonts release input')
    return refs[0]


def declared_language(tree):
    path = tree / 'craft-i18n.rev'
    if not path.exists():
        return None
    commit = path.read_text(encoding='utf-8').strip()
    if not COMMIT.fullmatch(commit):
        raise ValueError('Expected one immutable craft-i18n input')
    return commit


def member(tree, name):
    parts = PurePosixPath(name).parts
    if not parts or PurePosixPath(name).is_absolute() or '..' in parts or '\\' in name:
        raise ValueError('Unsafe resource manifest path')
    path = tree.joinpath(*parts)
    if not path.is_file() or path.is_symlink():
        raise ValueError('Missing resource input: ' + name)
    return path


def validate_fonts(tree):
    """Validate every supplied manifest row and retain all upstream OFL notices."""
    manifests = sorted((tree / 'fonts').glob('*manifest.txt'))
    if not manifests:
        raise ValueError('Missing font manifests')
    count = 0
    for manifest in manifests:
        for line in manifest.read_text(encoding='utf-8').splitlines():
            if not line.strip() or line.lstrip().startswith('#'):
                continue
            fields = [x.strip() for x in line.split('|')]
            if len(fields) != 8 or fields[4] != 'OFL-1.1' or not SHA.fullmatch(fields[6]):
                raise ValueError('Unknown font manifest/license; review required')
            font, license_file = member(tree, fields[2]), member(tree, fields[5])
            if hashlib.sha256(font.read_bytes()).hexdigest() != fields[6]:
                raise ValueError('Font bytes differ from upstream manifest')
            notice = license_file.read_text(encoding='utf-8-sig')
            if not re.search(r'SIL OPEN FONT LICENSE\s*[—–-]?\s*Version\s+1\.1', notice, re.IGNORECASE):
                raise ValueError('Missing declared OFL license text')
            count += 1
    if not count:
        raise ValueError('Empty font manifests')
    return count


def brand_catalogs(tree, original, name):
    """Adapt only UI catalog keys/values, never dictionaries or legal notices."""
    for catalog in (tree / 'locales').glob('*/*.tsv'):
        # The common catalogs serve other apps too; replace only this app's name.
        text = catalog.read_text(encoding='utf-8')
        updated = text.replace(original, name)
        if text != updated:
            catalog.write_text(updated, encoding='utf-8', newline='\n')
