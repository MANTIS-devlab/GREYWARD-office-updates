"""NOTICE is attribution to retain, not a byte-frozen application dependency.

Keep the first-party licensing/trademark statement stable. Third-party credits
may evolve freely: retain the complete new notice, pin it and report its diff.
This is not a semantic license checker; LICENSE files, dependency expressions
and resource locks have their own independent gates.
"""
import hashlib
import re


def _text(data):
    if not data or len(data) > 256 * 1024:
        raise ValueError('Missing/oversized upstream NOTICE')
    text = data.decode('utf-8-sig').replace('\r\n', '\n')
    if not text.strip() or any(ord(c) < 32 and c not in '\n\t' for c in text):
        raise ValueError('Invalid upstream NOTICE text')
    return text


def _terms(text):
    # The four reviewed upstream projects share this explicit section boundary.
    # Never guess a boundary for a newly structured NOTICE.
    sections = re.split(r'^Third-party material[ \t]*$', text, flags=re.MULTILINE)
    if len(sections) != 2 or not sections[0].strip() or not sections[1].strip():
        raise ValueError('NOTICE structure changed; first-party terms review required')
    header = sections[0]
    # Copyright years are metadata; holders and every other header word remain
    # significant. Only a leading year/range following the copyright marker is
    # normalized, never arbitrary numbers elsewhere in licensing terms.
    header = re.sub(r'(?im)^(Copyright\s+(?:\(c\)|©)\s+)\d{4}(?:[-–]\d{4})?(?=\s)',
                    r'\g<1><year>', header)
    return ' '.join(header.split())


def review_notice(old, new):
    old_text, new_text = _text(old), _text(new)
    if old != new and _terms(old_text) != _terms(new_text):
        raise ValueError('NOTICE first-party licensing/trademark terms changed; review required')
    return {'changed': old != new,
            'policy': 'retain-third-party-notices-v1',
            'oldSha256': hashlib.sha256(old).hexdigest(),
            'newSha256': hashlib.sha256(new).hexdigest()}
