"""Remove only proven-unused Git patch declarations before Cargo vendoring.

Cargo vendor omits [[patch.unused]] packages. Leaving their declarations makes
later offline metadata/builds try to find those absent packages in the vendor
directory. The resolved package graph stays byte-for-byte equivalent as TOML
data; original upstream manifests remain in the source RPM.
"""
import copy
import hashlib
import json
import re
import tomllib


def prune_unused(tree):
    manifest_path, lock_path = tree / 'Cargo.toml', tree / 'Cargo.lock'
    manifest_raw, lock_raw = manifest_path.read_bytes(), lock_path.read_bytes()
    manifest_text = manifest_raw.decode().replace('\r\n', '\n')
    lock_text = lock_raw.decode().replace('\r\n', '\n')
    manifest, lock = tomllib.loads(manifest_text), tomllib.loads(lock_text)
    unused = lock.get('patch', {}).get('unused', [])
    if not unused:
        return {'removed': []}
    active = {p['name'] for p in lock['package']}
    entries = manifest.get('patch', {}).get('crates-io', {})
    remove = {}
    for item in unused:
        candidates = [(alias, value) for alias, value in entries.items()
                      if isinstance(value, dict) and value.get('package', alias) == item['name']]
        if len(candidates) != 1 or item['name'] in active:
            raise ValueError('Unused patch cannot be proven outside the active graph')
        alias, value = candidates[0]
        revision = value.get('rev', '')
        expected = 'git+' + value.get('git', '') + '?rev=' + revision + '#' + revision
        if not re.fullmatch(r'[a-f0-9]{40}', revision) or item.get('source') != expected:
            raise ValueError('Unused patch is not pinned to its locked Git revision')
        remove[alias] = item
    expected_manifest = copy.deepcopy(manifest)
    for alias in remove:
        del expected_manifest['patch']['crates-io'][alias]
    lines, section, counts = [], None, {alias: 0 for alias in remove}
    for line in manifest_text.splitlines(True):
        if line.lstrip().startswith('['):
            section = line.strip()
        alias = next((name for name in remove if re.match(r'^' + re.escape(name) + r'\s*=', line)), None)
        if section == '[patch.crates-io]' and alias:
            if not re.fullmatch(r'[^=]+\s*=\s*\{[^\n]+\}\s*(?:#[^\n]*)?\n?', line):
                raise ValueError('Unsupported multiline unused patch declaration')
            counts[alias] += 1
        else:
            lines.append(line)
    new_manifest = ''.join(lines)
    if any(count != 1 for count in counts.values()) or tomllib.loads(new_manifest) != expected_manifest:
        raise ValueError('Cargo manifest change exceeded unused patch declarations')
    blocks = re.compile(r'^\[\[patch\.unused\]\]\n.*?(?=^\[|\Z)', re.MULTILINE | re.DOTALL)
    removed = []
    def drop(match):
        value = tomllib.loads(match[0])['patch']['unused']
        if len(value) != 1 or value[0] not in unused:
            raise ValueError('Unexpected unused lock entry')
        removed.extend(value)
        return ''
    new_lock = blocks.sub(drop, lock_text)
    expected_lock = copy.deepcopy(lock)
    expected_lock.pop('patch')
    if len(removed) != len(unused) or tomllib.loads(new_lock) != expected_lock:
        raise ValueError('Resolved Cargo package graph changed')
    backup = tree / 'greyward-cargo-upstream'
    backup.mkdir(exist_ok=False)
    (backup / 'Cargo.toml').write_bytes(manifest_raw)
    (backup / 'Cargo.lock').write_bytes(lock_raw)
    manifest_path.write_text(new_manifest, encoding='utf-8', newline='\n')
    lock_path.write_text(new_lock, encoding='utf-8', newline='\n')
    result = {'removed': removed,
              'upstreamManifestSha256': hashlib.sha256(manifest_raw).hexdigest(),
              'upstreamLockSha256': hashlib.sha256(lock_raw).hexdigest(),
              'preparedManifestSha256': hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
              'preparedLockSha256': hashlib.sha256(lock_path.read_bytes()).hexdigest()}
    (backup / 'preparation.json').write_text(json.dumps(result, indent=2) + '\n')
    return result
