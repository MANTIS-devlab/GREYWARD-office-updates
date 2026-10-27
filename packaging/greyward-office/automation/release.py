#!/usr/bin/env python3
"""Trusted orchestration: scoped GitHub state, sealed signing and finite expiry."""
import argparse
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

from github import GitHub, REPOSITORY
from model import UTC, TAG, SLUGS, LOG_RETENTION_DAYS, active, cleanup_plan, decision, instant, promote, timestamp, validate_policy, validate_state
from seal import digest, seal, validate_candidate, verify_family

HERE = Path(__file__).resolve().parent
STATE = 'state/office.json'
POINTER = 'channel/f44-x86_64/mirrorlist.txt'


def encode(value): return (json.dumps(value, indent=2, sort_keys=True) + '\n').encode()


def load_policy(repo):
    policy = json.loads((repo / 'packaging/greyward-office/automation/policy.json').read_text())
    validate_policy(policy)
    return policy


def load_state(api):
    state = json.loads(api.content(STATE)); validate_state(state)
    return state


def snapshot_url(commit, tag):
    if not re.fullmatch('[a-f0-9]{40}', commit) or not TAG.fullmatch(tag):
        raise ValueError('Invalid metadata snapshot identity')
    return f'https://raw.githubusercontent.com/{REPOSITORY}/{commit}/snapshots/{tag}/'


def assert_current_channel(api, state):
    if not state.get('current'): return
    if state.get('channelVerified') != state['current']:
        raise ValueError('Unverified channel cannot authorize cleanup')
    expected = (snapshot_url(state['metadataCommit'], state['current']) + '\n').encode()
    if api.content(POINTER) != expected:
        raise ValueError('Channel pointer changed; refuse deletion')


def clean(api, policy, state, now, apply=False):
    validate_policy(policy)
    if apply and not policy['enabled']:
        raise ValueError('Cleanup policy is disabled')
    releases = api.releases()
    plan = cleanup_plan(policy, state, releases, now)
    pending = state.get('pending')
    if pending and (now - instant(pending['createdAt'])).total_seconds() >= policy['graceHours'] * 3600:
        remote = next((r for r in releases if r['id'] == pending['releaseId']), None)
        if remote and remote['tag_name'] != pending['tag']:
            raise ValueError('Pending release identity changed')
        if pending['tag'] == state.get('current'):
            raise ValueError('Pending release is currently offered')
        plan.append({'releaseId': pending['releaseId'], 'tag': pending['tag'],
                     'releaseExists': remote is not None, 'pending': True})
    if apply and plan:
        assert_current_channel(api, state)
        result = copy.deepcopy(state)
        for item in plan:
            if item['releaseExists']: api.request('/releases/' + str(item['releaseId']), 'DELETE')
            api.request('/git/refs/tags/' + item['tag'], 'DELETE', missing=True)
            if item.get('pending'): result['pending'] = None
            else: result['generations'] = [g for g in result['generations'] if g['tag'] != item['tag']]
        api.commit({STATE: encode(result)}, 'Expire verified obsolete Office generations')
        return result, plan
    return state, plan


def gate(repo, api, now, force=False, pilot=False):
    policy = load_policy(repo); state = load_state(api)
    if not pilot and policy['enabled']:
        expire_ci_storage(api, now, os.environ['GITHUB_RUN_ID'])
    if not pilot and policy['enabled'] and (not state['current'] or state.get('channelVerified') == state['current']):
        state, _ = clean(api, policy, state, now, apply=True)
    effective = dict(policy, enabled=True) if pilot else policy
    result = decision(effective, state, now, force)
    initial = state['current'] is None
    result['verify'] = result['reason'] == 'channel-unverified' and not pilot
    lock = json.loads((repo / 'packaging/greyward-office/source.json').read_text())
    result['bootstrap'] = initial or digest(repo / 'packaging/greyward-office/source.json') != state.get('sourceManifestSha256')
    result['generation'] = max(lock['rpmRelease'] + (0 if initial else 1), state['nextGeneration'])
    if result['due'] and not pilot:
        state['lastAttempt'] = timestamp(now)
        state['nextGeneration'] = result['generation'] + 1
        state['attemptRun'] = os.environ['GITHUB_RUN_ID']
        state['attemptCycle'] = result['cycle']
        state['reservedGeneration'] = result['generation']
        api.commit({STATE: encode(state)}, 'Claim six-day Office verification cycle')
    if result['due']: check_certificate(repo, now)
    if result['verify']:
        last = state.get('lastVerificationAttempt')
        if last and (now - instant(last)).total_seconds() < policy['retryHours'] * 3600:
            result['verify'] = False
        else:
            state['lastVerificationAttempt'] = timestamp(now)
            api.commit({STATE: encode(state)}, 'Claim interrupted Office channel verification')
    return result


def check_certificate(repo, now):
    public = repo / 'packaging/greyward-office/keys/RPM-GPG-KEY-greyward-office.gpg'
    rows = subprocess.check_output(['gpg', '--batch', '--with-colons', '--show-keys', str(public)], text=True)
    keys = [line.split(':') for line in rows.splitlines() if line.startswith(('pub:', 'sub:'))]
    deadline = int(now.timestamp()) + 30 * 24 * 3600
    primaries = [r for r in keys if r[0] == 'pub']
    signing = [r for r in keys if r[0] == 'sub' and 's' in r[11] and r[1] not in ('r', 'e', 'd')]
    if len(primaries) != 1 or primaries[0][1] in ('r', 'e', 'd') or not signing:
        raise ValueError('Expected one Office certificate and a dedicated signing subkey')
    if (primaries[0][6] and int(primaries[0][6]) <= deadline) or not any(not r[6] or int(r[6]) > deadline for r in signing):
        raise ValueError('Office signing certificate/subkey expires within 30 days; renew before promotion')


def expire_ci_storage(api, now, current_run):
    """Own completed pipeline artifacts immediately; own CI logs after 14 days."""
    owned_paths = {'.github/workflows/office.yml', '.github/workflows/validation.yml'}
    runs = {}
    def owned(run):
        return (str(run['id']) != str(current_run) and run.get('status') == 'completed'
                and run.get('path') in owned_paths and run.get('head_branch') == 'main'
                and run.get('head_repository', {}).get('full_name') == REPOSITORY)
    def listing(endpoint, field):
        result = []
        for page in range(1, 11):
            values = api.request(endpoint + '&page=' + str(page))[field]
            result.extend(values)
            if len(values) < 100: return result
        raise ValueError('CI inventory exceeds bounded pagination')
    # Inventory first, then mutate: deleting while paginating would skip entries.
    for item in listing('/actions/artifacts?per_page=100', 'artifacts'):
        if item['name'] not in {'office-candidate', *('office-build-' + s for s in SLUGS)}:
            continue
        run_id = item.get('workflow_run', {}).get('id')
        if not run_id or str(run_id) == str(current_run): continue
        if run_id not in runs: runs[run_id] = api.request('/actions/runs/' + str(run_id))
        if owned(runs[run_id]): api.request('/actions/artifacts/' + str(item['id']), 'DELETE')
    from datetime import timedelta
    cutoff = timestamp(now - timedelta(days=LOG_RETENTION_DAYS))
    from urllib.parse import quote
    for run in listing('/actions/runs?per_page=100&created=' + quote('<' + cutoff, safe=''), 'workflow_runs'):
        if owned(run) and instant(run['created_at']) < instant(cutoff):
            api.request('/actions/runs/' + str(run['id']), 'DELETE')


def checkpoint(api, now):
    state = load_state(api)
    if state.get('attemptRun') != os.environ['GITHUB_RUN_ID']:
        raise ValueError('No-change check does not own the claimed cycle')
    state['completedCycle'] = max(state['completedCycle'], state['attemptCycle'])
    state['lastVerifiedAt'] = timestamp(now)
    api.commit({STATE: encode(state)}, 'Office upstream check complete; no new generation')


def command(argv, env=None):
    subprocess.run(list(map(str, argv)), env=env, check=True, timeout=1200)


def key(repo, home):
    policy = json.loads((repo / 'packaging/greyward-office/channel.json').read_text())
    public = repo / 'packaging/greyward-office/keys/RPM-GPG-KEY-greyward-office.gpg'
    if digest(public) != policy['keySha256']:
        raise ValueError('Public distribution key drift')
    fingerprint = policy['fingerprint']
    if not re.fullmatch('[A-F0-9]{40}|[A-F0-9]{64}', fingerprint): raise ValueError('Invalid pinned fingerprint')
    env = dict(os.environ, GNUPGHOME=str(home))
    listed = subprocess.check_output(['gpg', '--batch', '--with-colons', '--fingerprint', fingerprint], env=env, text=True)
    if not any(l.startswith('fpr:') and l.split(':')[9] == fingerprint for l in listed.splitlines()):
        raise ValueError('Expected distribution certificate is missing')
    return fingerprint, public, env


def sign_bundle(repo, bundle, home):
    fingerprint, public, env = key(repo, home)
    inventory = json.loads((bundle / 'inventory.json').read_text())
    for name, sha in inventory['files'].items():
        if Path(name).name != name or (bundle / name).is_symlink() or digest(bundle / name) != sha:
            raise ValueError('Sealed artifact drift before signing')
    verify_family(bundle / 'recipe', list(bundle.glob('*.x86_64.rpm')))
    for rpm in sorted(bundle.glob('*.rpm')):
        command(['rpmsign', '--define', '_openpgp_sign_id ' + fingerprint, '--addsign', rpm], env)
        inventory['files'][rpm.name] = digest(rpm)
    shutil.copyfile(public, bundle / 'RPM-GPG-KEY-greyward-office')
    inventory['files']['RPM-GPG-KEY-greyward-office'] = digest(public)
    (bundle / 'inventory.json').write_bytes(encode(inventory))
    command(['gpg', '--batch', '--armor', '--local-user', fingerprint, '--detach-sign', bundle / 'inventory.json'], env)
    return fingerprint, env


def owned_inventory(api, generation, directory, env):
    prefix = f'https://github.com/{REPOSITORY}/releases/download/{generation["tag"]}/'
    directory.mkdir(parents=True, exist_ok=False)
    api.download(prefix + 'inventory.json', directory / 'inventory.json', generation['inventorySha256'])
    api.download(prefix + 'inventory.json.asc', directory / 'inventory.json.asc')
    command(['gpg', '--batch', '--verify', directory / 'inventory.json.asc', directory / 'inventory.json'], env)
    inventory = json.loads((directory / 'inventory.json').read_text())
    if inventory.get('generation') != generation['generation']:
        raise ValueError('Retained signed inventory generation mismatch')
    for name, sha in inventory['files'].items():
        if Path(name).name != name or not re.fullmatch('[a-f0-9]{64}', sha): raise ValueError('Unsafe retained asset')
        if name.endswith('.x86_64.rpm'): api.download(prefix + name, directory / name, sha)
    return directory


def metadata(directory, fingerprint, env, baseurl=None):
    args = ['createrepo_c', '--simple-md-filenames', '--database']
    if baseurl: args += ['--baseurl', baseurl]
    command([*args, directory])
    command(['gpg', '--batch', '--armor', '--local-user', fingerprint, '--detach-sign', directory / 'repodata/repomd.xml'], env)


def native_test(repository, home, fingerprint, output, previous=None, remote=None):
    spec = importlib.util.spec_from_file_location('native_office_dnf', HERE / 'dnf-check.py')
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module.check(repository, home, fingerprint, output, previous, remote)


def upload(api, release, files):
    present = {a['name']: a for a in release['assets']}
    for path in files:
        if path.stat().st_size >= 2 * 1024 ** 3: raise ValueError('Asset exceeds GitHub limit')
        sha = digest(path)
        old = present.get(path.name)
        if old:
            if old.get('digest') != 'sha256:' + sha:
                raise ValueError('Existing release asset bytes differ; never overwrite')
            continue
        # Upload API expects the unencoded plain asset name as a query parameter.
        from urllib.parse import quote
        result = api.request('/releases/' + str(release['id']) + '/assets?name=' + quote(path.name),
                             'POST', path.read_bytes(), binary=True, upload=True)
        if result.get('digest') != 'sha256:' + sha:
            raise ValueError('Uploaded asset digest mismatch')


def publish(repo, api, incoming, candidate, output, home, now):
    policy = load_policy(repo)
    if not policy['enabled']: raise ValueError('Publication policy is disabled')
    state = load_state(api)
    if state.get('attemptRun') != os.environ['GITHUB_RUN_ID']:
        raise ValueError('Publisher does not own the claimed cycle')
    lock = json.loads(candidate.read_text())
    if lock['rpmRelease'] != state.get('reservedGeneration'): raise ValueError('Candidate reservation mismatch')
    tag = 'office-f44-x86_64-g' + str(lock['rpmRelease'])
    bundle = output / 'bundle'
    seal(repo, incoming, candidate, bundle)
    fingerprint, env = sign_bundle(repo, bundle, home)
    previous = None
    if state['current']:
        previous = owned_inventory(api, next(g for g in state['generations'] if g['tag'] == state['current']), output / 'previous', env)
    # Local native signature/order checks precede any public artifact.
    local = output / 'local-repository'; local.mkdir()
    for p in bundle.glob('*.x86_64.rpm'): shutil.copyfile(p, local / p.name)
    shutil.copyfile(bundle / 'RPM-GPG-KEY-greyward-office', local / 'RPM-GPG-KEY-greyward-office')
    metadata(local, fingerprint, env)
    native_test(local, home, fingerprint, output / 'dnf-local', previous)
    shutil.copyfile(output / 'dnf-local/results.json', bundle / 'dnf-native.json')
    inventory = json.loads((bundle / 'inventory.json').read_text())
    inventory['files']['dnf-native.json'] = digest(bundle / 'dnf-native.json')
    (bundle / 'inventory.json').write_bytes(encode(inventory))
    command(['gpg', '--batch', '--yes', '--armor', '--local-user', fingerprint, '--detach-sign', bundle / 'inventory.json'], env)
    releases = api.releases()
    release = next((r for r in releases if r['tag_name'] == tag), None)
    if release and (not state.get('pending') or state['pending']['releaseId'] != release['id']):
        raise ValueError('Refuse to adopt an untracked existing release')
    if not release:
        release = api.request('/releases', 'POST', {'tag_name': tag, 'target_commitish': 'main',
            'name': 'GREYWARD Office generation ' + str(lock['rpmRelease']), 'draft': True,
            'prerelease': True, 'make_latest': 'false',
            'body': 'Automated GREYWARD Office build, based on the ArtCraft projects. '
                    'Availability in Update Center is controlled by the signed channel, not this release page.'})
        state['pending'] = {'tag': tag, 'releaseId': release['id'], 'createdAt': timestamp(now)}
        api.commit({STATE: encode(state)}, 'Track candidate Office release before uploads')
    files = [p for p in bundle.iterdir() if p.is_file()]
    upload(api, release, files)
    # Unsigned transport material must never become a public release asset.
    for item in release['assets']:
        if item['name'].startswith(tuple('stage-' + s + '--' for s in SLUGS)):
            api.request('/releases/assets/' + str(item['id']), 'DELETE')
    if release['draft']:
        api.request('/releases/' + str(release['id']), 'PATCH', {'draft': False, 'prerelease': True, 'make_latest': 'false'})
    # The generation is public but not offered until every anonymous byte is verified.
    for p in files:
        api.download(f'https://github.com/{REPOSITORY}/releases/download/{tag}/{p.name}', output / ('readback-' + p.name), digest(p))
    generation = {'generation': lock['rpmRelease'], 'tag': tag, 'releaseId': release['id'],
                  'publishedAt': timestamp(now), 'inventorySha256': digest(bundle / 'inventory.json')}
    result = promote(policy, state, generation, state['attemptCycle'], now)
    combined = output / 'channel'; combined.mkdir()
    for g in active(result):
        source = bundle if g['tag'] == tag else owned_inventory(api, g, output / ('retained-' + g['tag']), env)
        target = combined / g['tag']; target.mkdir()
        for p in source.glob('*.x86_64.rpm'): shutil.copyfile(p, target / p.name)
    metadata(combined, fingerprint, env, 'https://github.com/' + REPOSITORY + '/releases/download/')
    generated = {f'snapshots/{tag}/repodata/' + p.name: p.read_bytes() for p in (combined / 'repodata').iterdir() if p.is_file()}
    snapshot = api.commit(generated, 'Stage signed Office metadata snapshot ' + tag)
    url = snapshot_url(snapshot, tag)
    # Test anonymous GitHub metadata/redirects with real DNF before switching the pointer.
    native_test(local, home, fingerprint, output / 'dnf-remote', previous, url)
    result['metadataCommit'] = snapshot
    result['channelVerified'] = None
    result['lastVerifiedAt'] = timestamp(now)
    result['sourceManifestSha256'] = digest(candidate)
    api.commit({STATE: encode(result), POINTER: (url + '\n').encode(),
                'packaging/greyward-office/source.json': candidate.read_bytes()}, 'Promote verified Office generation ' + tag)
    native_test(local, home, fingerprint, output / 'dnf-mirrorlist', previous,
                f'https://raw.githubusercontent.com/{REPOSITORY}/main/' + POINTER)
    result['channelVerified'] = tag
    api.commit({STATE: encode(result)}, 'Confirm native DNF discovery through the public channel')
    assert_current_channel(api, result)
    api.request('/releases/' + str(release['id']), 'PATCH', {'prerelease': False, 'make_latest': 'true'})
    return {'generation': tag, 'retained': [g['tag'] for g in active(result)],
            'cleanupAfterGrace': [g['tag'] for g in result['generations'] if g.get('withdrawnAt')]}


def verify_channel(repo, api, output, home):
    state = load_state(api)
    fingerprint, _, env = key(repo, home)
    current = next(g for g in state['generations'] if g['tag'] == state['current'])
    local = owned_inventory(api, current, output / 'current', env)
    shutil.copyfile(repo / 'packaging/greyward-office/keys/RPM-GPG-KEY-greyward-office.gpg', local / 'RPM-GPG-KEY-greyward-office')
    metadata(local, fingerprint, env)
    old = [g for g in active(state) if g['generation'] < current['generation']]
    previous = owned_inventory(api, old[0], output / 'previous', env) if old else None
    native_test(local, home, fingerprint, output / 'dnf-reverify', previous,
                f'https://raw.githubusercontent.com/{REPOSITORY}/main/' + POINTER)
    state['channelVerified'] = state['current']
    api.commit({STATE: encode(state)}, 'Recover interrupted Office channel verification')
    assert_current_channel(api, state)
    api.request('/releases/' + str(current['releaseId']), 'PATCH', {'prerelease': False, 'make_latest': 'true'})
    return {'verified': state['current']}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('operation', choices=('gate', 'checkpoint', 'cleanup', 'publish', 'verify'))
    p.add_argument('--repo', type=Path, default=Path.cwd()); p.add_argument('--force', action='store_true')
    p.add_argument('--pilot', action='store_true'); p.add_argument('--apply', action='store_true')
    p.add_argument('--incoming', type=Path); p.add_argument('--candidate', type=Path)
    p.add_argument('--output', type=Path); p.add_argument('--home', type=Path)
    a = p.parse_args(); a.repo = a.repo.resolve()
    import datetime as dt
    now = dt.datetime.now(UTC); api = GitHub()
    if os.environ.get('GITHUB_REPOSITORY') != REPOSITORY or os.environ.get('GITHUB_REF') != 'refs/heads/main':
        raise ValueError('Distribution operations require the owned main workflow')
    if a.operation == 'gate': result = gate(a.repo, api, now, a.force, a.pilot)
    elif a.operation == 'checkpoint': checkpoint(api, now); result = {'checkpoint': True}
    elif a.operation == 'cleanup':
        policy = load_policy(a.repo)
        if a.apply and not policy['enabled']: raise ValueError('Cleanup disabled')
        _, result = clean(api, policy, load_state(api), now, a.apply)
    elif a.operation == 'verify':
        if not a.output or not a.home: p.error('Verification paths required')
        a.output.mkdir(parents=True, exist_ok=False)
        result = verify_channel(a.repo, api, a.output.resolve(), a.home.resolve())
    else:
        if not all((a.incoming, a.candidate, a.output, a.home)): p.error('Publication paths required')
        a.output.mkdir(parents=True, exist_ok=False)
        result = publish(a.repo, api, a.incoming.resolve(), a.candidate.resolve(), a.output.resolve(), a.home.resolve(), now)
    print(json.dumps(result))
    if a.operation == 'gate' and os.environ.get('GITHUB_OUTPUT'):
        with open(os.environ['GITHUB_OUTPUT'], 'a') as stream:
            for key in ('due', 'cycle', 'generation', 'bootstrap', 'verify'):
                stream.write(key + '=' + json.dumps(result[key]) + '\n')


if __name__ == '__main__': main()
