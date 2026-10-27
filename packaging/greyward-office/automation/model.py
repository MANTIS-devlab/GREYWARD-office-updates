"""Deterministic Office cadence/retention decisions; no I/O or credentials."""
import copy
import datetime as dt
import re

UTC = dt.timezone.utc
SLUGS = ('writer', 'sheets', 'pdf', 'slides')
TAG = re.compile(r'office-f44-x86_64-g([1-9][0-9]{0,8})\Z')
SHA = re.compile(r'[0-9a-f]{64}\Z')


def instant(value):
    if not isinstance(value, str):
        raise ValueError('UTC timestamp required')
    result = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
    if result.utcoffset() != dt.timedelta(0):
        raise ValueError('UTC timestamp required')
    return result


def timestamp(value):
    if value.tzinfo is None:
        raise ValueError('Timezone required')
    return value.astimezone(UTC).isoformat(timespec='seconds').replace('+00:00', 'Z')


def validate_policy(policy):
    expected = {'schema': 'greyward.office-automation-policy/v1',
                'repository': 'MANTIS-devlab/GREYWARD-office-updates',
                'periodHours': 144, 'retryHours': 24, 'keepGenerations': 3,
                'graceHours': 48, 'fedora': 44, 'architecture': 'x86_64'}
    if (set(policy) != set(expected) | {'enabled'} or type(policy.get('enabled')) is not bool
            or any(policy.get(k) != v for k, v in expected.items())):
        raise ValueError('Unreviewed automation policy')


def new_state(epoch):
    instant(epoch)
    return {'schema': 'greyward.office-automation-state/v1', 'epoch': epoch,
            'completedCycle': -1, 'lastAttempt': None, 'current': None,
            'generations': [], 'pending': None, 'nextGeneration': 1}


def validate_state(state):
    if (state.get('schema') != 'greyward.office-automation-state/v1'
            or type(state.get('completedCycle')) is not int or state['completedCycle'] < -1):
        raise ValueError('Invalid automation state')
    instant(state['epoch'])
    if state.get('lastAttempt'):
        instant(state['lastAttempt'])
    seen = set()
    for item in state['generations']:
        match = TAG.fullmatch(item.get('tag', ''))
        if (not match or item.get('generation') != int(match[1]) or item['tag'] in seen
                or type(item.get('releaseId')) is not int or item['releaseId'] < 1
                or not SHA.fullmatch(item.get('inventorySha256', ''))):
            raise ValueError('Invalid/duplicate owned generation')
        instant(item['publishedAt'])
        if item.get('withdrawnAt'):
            instant(item['withdrawnAt'])
        seen.add(item['tag'])
    if state.get('current') is not None and state['current'] not in seen:
        raise ValueError('Current generation is missing')
    if state.get('pending'):
        pending = state['pending']
        if not TAG.fullmatch(pending.get('tag', '')) or type(pending.get('releaseId')) is not int:
            raise ValueError('Invalid pending draft')
        instant(pending['createdAt'])
    if type(state.get('nextGeneration')) is not int or state['nextGeneration'] < 1:
        raise ValueError('Invalid generation reservation')


def active(state):
    validate_state(state)
    return sorted((g for g in state['generations'] if not g.get('withdrawnAt')),
                  key=lambda g: g['generation'], reverse=True)


def decision(policy, state, now, force=False):
    validate_policy(policy)
    validate_state(state)
    if not policy['enabled']:
        return {'due': False, 'reason': 'disabled', 'cycle': -1}
    elapsed = (now - instant(state['epoch'])).total_seconds()
    cycle = int(elapsed // (policy['periodHours'] * 3600))
    if cycle < 0:
        return {'due': False, 'reason': 'before-epoch', 'cycle': cycle}
    if state.get('current') and state.get('channelVerified') != state['current']:
        return {'due': False, 'reason': 'channel-unverified', 'cycle': cycle}
    # A prior withdrawal must finish before another promotion can exceed four.
    if any(g.get('withdrawnAt') for g in state['generations']):
        return {'due': False, 'reason': 'cleanup-pending', 'cycle': cycle}
    if state.get('pending'):
        return {'due': False, 'reason': 'draft-pending', 'cycle': cycle}
    if not force and cycle <= state['completedCycle']:
        return {'due': False, 'reason': 'not-due', 'cycle': cycle}
    if (not force and state.get('lastAttempt') and
            now - instant(state['lastAttempt']) < dt.timedelta(hours=policy['retryHours'])):
        return {'due': False, 'reason': 'retry-backoff', 'cycle': cycle}
    return {'due': True, 'reason': 'manual' if force else 'six-day-cycle', 'cycle': cycle}


def promote(policy, state, generation, cycle, now):
    validate_policy(policy)
    validate_state(state)
    result = copy.deepcopy(state)
    existing = next((g for g in result['generations'] if g['tag'] == generation['tag']), None)
    if existing:
        if existing != generation:
            raise ValueError('Published generation cannot change')
        return result
    if any(g.get('withdrawnAt') for g in result['generations']):
        raise ValueError('Previous cleanup must finish first')
    if result['generations'] and generation['generation'] <= max(g['generation'] for g in result['generations']):
        raise ValueError('Generation ordering must increase')
    result['generations'].append(copy.deepcopy(generation))
    result['current'] = generation['tag']
    result['pending'] = None
    result['completedCycle'] = max(cycle, result['completedCycle'])
    for old in active(result)[policy['keepGenerations']:]:
        next(g for g in result['generations'] if g['tag'] == old['tag'])['withdrawnAt'] = timestamp(now)
    validate_state(result)
    return result


def cleanup_plan(policy, state, releases, now):
    """Return exact release/tag IDs; never select an untracked remote release."""
    validate_policy(policy)
    validate_state(state)
    by_id = {r['id']: r for r in releases}
    keep = {g['tag'] for g in active(state)[:policy['keepGenerations']]}
    result = []
    for g in state['generations']:
        if not g.get('withdrawnAt') or g['tag'] in keep or g['tag'] == state['current']:
            continue
        if now - instant(g['withdrawnAt']) < dt.timedelta(hours=policy['graceHours']):
            continue
        remote = by_id.get(g['releaseId'])
        # Missing release is a recoverable partially completed deletion.
        if remote and (remote['tag_name'] != g['tag'] or remote.get('draft')):
            raise ValueError('Remote release identity changed; refuse cleanup')
        result.append({'releaseId': g['releaseId'], 'tag': g['tag'], 'releaseExists': remote is not None})
    return result


def exact_adaptations(patch, files):
    """Apply only unique verbatim reviewed hunks; no fuzzy/regex substitutions.

    Offset-only upstream changes outside a hunk are permitted. Any missing or
    ambiguous hunk, path change, unsupported patch syntax or binary edit blocks.
    Return expected output bytes; caller must compare with real git apply output.
    """
    lines = patch.decode('utf-8').splitlines(keepends=True)
    output = dict(files)
    current = None
    covered = set()
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith('diff --git '):
            match = re.fullmatch(r'diff --git a/([^\s]+) b/([^\s]+)\n', line)
            if not match or match[1] != match[2] or match[1] not in files:
                raise ValueError('Unapproved patch path')
            current = match[1]
            if current.startswith('/') or '..' in current.split('/') or '\\' in current:
                raise ValueError('Unsafe patch path')
            covered.add(current)
        elif line.startswith('@@ '):
            if current is None:
                raise ValueError('Patch hunk without file')
            match = re.fullmatch(r'@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@[^\n]*\n', line)
            if not match:
                raise ValueError('Malformed hunk')
            old, new = [], []
            i += 1
            while i < len(lines) and not lines[i].startswith(('@@ ', 'diff --git ')):
                item = lines[i]
                if item[:1] not in (' ', '+', '-'):
                    raise ValueError('Unsupported patch content')
                if item[0] in ' -': old.append(item[1:])
                if item[0] in ' +': new.append(item[1:])
                i += 1
            if len(old) != int(match[2] if match[2] is not None else 1) or len(new) != int(match[4] if match[4] is not None else 1):
                raise ValueError('Patch hunk length mismatch')
            before, after = ''.join(old).encode(), ''.join(new).encode()
            if not before or output[current].count(before) != 1:
                raise ValueError('Missing or ambiguous exact adaptation: ' + current)
            output[current] = output[current].replace(before, after, 1)
            continue
        elif not line.startswith(('index ', '--- a/', '+++ b/')):
            raise ValueError('Unsupported patch header')
        i += 1
    if covered != set(files):
        raise ValueError('Adaptation inventory does not match reviewed patch paths')
    return output
