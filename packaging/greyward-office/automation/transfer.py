#!/usr/bin/env python3
"""Trusted credentialed transfer after an unprivileged, credential-free build.

Keep one app's binary+source in Actions storage at a time. Final signing is a
separate job; staging never imports a signing key or executes a produced binary.
"""
import argparse
import datetime as dt
import json
import os
from pathlib import Path

from github import GitHub
from model import UTC, SLUGS, MAX_ARTIFACT_BYTES, timestamp
from release import STATE, encode, load_state, upload
from seal import digest, validate_candidate


def expected_files(lock, slug):
    app = next(a for a in lock['applications'] if a['slug'] == slug)
    base = f'{app["package"]}-{app["version"]}-{lock["rpmRelease"]}.fc44.'
    return {base + 'x86_64.rpm', base + 'src.rpm', slug + '-engine.json', slug + '-gui.json', slug + '-builder-packages.txt'}


def validate_transfer(repo, incoming, candidate, slug):
    if slug not in SLUGS: raise ValueError('Unexpected app')
    lock = json.loads(candidate.read_text())
    validate_candidate(json.loads((repo / 'packaging/greyward-office/source.json').read_text()), lock)
    files = list(incoming.iterdir())
    if {p.name for p in files} != expected_files(lock, slug) or any(not p.is_file() or p.is_symlink() for p in files):
        raise ValueError('Build output must be exactly the regular expected five files')
    # Avoid exceeding the free Actions artifact allowance even as sources grow.
    if sum(p.stat().st_size for p in files) > MAX_ARTIFACT_BYTES:
        raise ValueError('One-app artifact exceeds the reviewed free-storage budget')
    engine = json.loads((incoming / (slug + '-engine.json')).read_text())
    gui = json.loads((incoming / (slug + '-gui.json')).read_text())
    if set(engine) != {slug} or not engine[slug] or any(v is not True for v in engine[slug].values()):
        raise ValueError('Engine did not pass')
    if gui.get('private_x11_startup') is not True or gui.get('no_tcp_udp_listener') is not True:
        raise ValueError('GUI/listener check did not pass')
    return lock, files


def stage(api, repo, incoming, candidate, slug, artifact_id, pilot=False):
    lock, files = validate_transfer(repo, incoming, candidate, slug)
    artifact = api.request('/actions/artifacts/' + str(artifact_id))
    if artifact.get('name') != 'office-build-' + slug or str(artifact.get('workflow_run', {}).get('id')) != os.environ['GITHUB_RUN_ID']:
        raise ValueError('Artifact does not belong to this trusted workflow run')
    if not pilot:
        state = load_state(api)
        if state.get('attemptRun') != os.environ['GITHUB_RUN_ID'] or state.get('reservedGeneration') != lock['rpmRelease']:
            raise ValueError('Staging does not own the reserved generation')
        tag = 'office-f44-x86_64-g' + str(lock['rpmRelease'])
        release = next((r for r in api.releases() if r['tag_name'] == tag), None)
        if not release:
            release = api.request('/releases', 'POST', {'tag_name': tag, 'target_commitish': 'main', 'draft': True,
                'prerelease': True, 'make_latest': 'false', 'name': 'GREYWARD Office generation ' + str(lock['rpmRelease']),
                'body': 'Private staging; not an offered GREYWARD Office update.'})
            state['pending'] = {'tag': tag, 'releaseId': release['id'], 'createdAt': timestamp(dt.datetime.now(UTC))}
            api.commit({STATE: encode(state)}, 'Track bounded Office staging draft')
        if not release['draft'] or not state.get('pending') or state['pending']['releaseId'] != release['id']:
            raise ValueError('Refuse to stage into an untracked/non-draft release')
        # Copies get a distinct prefix so signed assets never overwrite staged bytes.
        staged = incoming.parent / ('stage-' + slug); staged.mkdir(exist_ok=False)
        import shutil
        for source in [*files, candidate]:
            name = 'source.json' if source == candidate else source.name
            shutil.copyfile(source, staged / ('stage-' + slug + '--' + name))
        upload(api, release, list(staged.iterdir()))
    # This delete is scoped to the owned build artifact, never another run.
    api.request('/actions/artifacts/' + str(artifact_id), 'DELETE')
    return {'application': slug, 'pilot': pilot, 'artifact_removed': True,
            'files': {p.name: digest(p) for p in files}}


def fetch(api, candidate, output):
    lock = json.loads(candidate.read_text())
    state = load_state(api)
    if not state.get('pending') or state.get('attemptRun') != os.environ['GITHUB_RUN_ID']:
        raise ValueError('No owned staging release')
    release = api.request('/releases/' + str(state['pending']['releaseId']))
    if not release['draft'] or release['tag_name'] != 'office-f44-x86_64-g' + str(lock['rpmRelease']):
        raise ValueError('Staging identity changed')
    assets = {a['name']: a for a in release['assets']}
    output.mkdir(parents=True, exist_ok=False)
    for slug in SLUGS:
        for name in expected_files(lock, slug) | {'source.json'}:
            asset = assets.get('stage-' + slug + '--' + name)
            if not asset: raise ValueError('Incomplete staging generation')
            target = output / (slug + '-source.json' if name == 'source.json' else name)
            api.staged_asset(asset, target)
            if name == 'source.json' and target.read_bytes() != candidate.read_bytes():
                raise ValueError('Mixed staged candidate manifests')


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('operation', choices=('stage', 'fetch'))
    p.add_argument('--repo', type=Path, default=Path.cwd()); p.add_argument('--incoming', type=Path)
    p.add_argument('--candidate', type=Path, required=True); p.add_argument('--slug', choices=SLUGS)
    p.add_argument('--artifact-id', type=int); p.add_argument('--pilot', action='store_true'); p.add_argument('--output', type=Path)
    a = p.parse_args(); api = GitHub()
    if a.operation == 'fetch': fetch(api, a.candidate, a.output)
    else: print(json.dumps(stage(api, a.repo.resolve(), a.incoming, a.candidate, a.slug, a.artifact_id, a.pilot)))
