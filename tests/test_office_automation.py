import copy
import io
import datetime as dt
import importlib.util
import json
from pathlib import Path
import sys
import unittest
import tempfile
import hashlib
import urllib.error
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
AUTOMATION = ROOT / 'packaging/greyward-office/automation'
sys.path.insert(0, str(AUTOMATION))
import model


class CadenceTests(unittest.TestCase):
    def setUp(self):
        self.policy = json.loads((AUTOMATION / 'policy.json').read_text())
        self.policy['enabled'] = True
        self.state = model.new_state('2026-01-28T03:23:00Z')
        self.start = model.instant(self.state['epoch'])

    def test_initial_disabled_and_before_epoch(self):
        self.assertTrue(model.decision(self.policy, self.state, self.start)['due'])
        self.policy['enabled'] = False
        self.assertFalse(model.decision(self.policy, self.state, self.start, force=True)['due'])
        self.policy['enabled'] = True
        self.assertFalse(model.decision(self.policy, self.state, self.start - dt.timedelta(seconds=1))['due'])

    def test_elapsed_six_days_crosses_month_without_reset(self):
        self.state['completedCycle'] = 0
        self.assertFalse(model.decision(self.policy, self.state, self.start + dt.timedelta(hours=143, minutes=59))['due'])
        decision = model.decision(self.policy, self.state, self.start + dt.timedelta(hours=144))
        self.assertTrue(decision['due'])
        self.assertEqual(decision['cycle'], 1)

    def test_outage_catches_up_once_without_replaying_old_cycles(self):
        self.state['completedCycle'] = 0
        decision = model.decision(self.policy, self.state, self.start + dt.timedelta(days=31))
        self.assertEqual(decision['cycle'], 5)
        self.state['completedCycle'] = 5
        self.assertFalse(model.decision(self.policy, self.state, self.start + dt.timedelta(days=31))['due'])

    def test_failed_attempt_daily_backoff_does_not_complete_cycle(self):
        self.state['lastAttempt'] = model.timestamp(self.start)
        self.assertFalse(model.decision(self.policy, self.state, self.start + dt.timedelta(hours=23))['due'])
        self.assertTrue(model.decision(self.policy, self.state, self.start + dt.timedelta(hours=24))['due'])
        self.assertEqual(self.state['completedCycle'], -1)

    def test_policy_cannot_change_scope_or_disable_safety(self):
        for key, value in [('repository', 'other/repo'), ('keepGenerations', 0), ('graceHours', 0)]:
            altered = dict(self.policy, **{key: value})
            with self.assertRaises(ValueError): model.validate_policy(altered)


def generation(number, when='2026-01-01T00:00:00Z'):
    return {'generation': number, 'tag': f'office-f44-x86_64-g{number}', 'releaseId': number + 100,
            'tagCommit': 'f' * 40, 'publishedAt': when, 'inventorySha256': str(number % 10) * 64}


class RetentionTests(CadenceTests):
    def populated(self, count):
        state = self.state
        for i in range(1, count + 1):
            state = model.promote(self.policy, state, generation(i), i, self.start)
            state['channelVerified'] = state['current']
        return state

    def test_fourth_promotion_withdraws_only_oldest(self):
        state = self.populated(4)
        self.assertEqual([g['generation'] for g in model.active(state)], [4, 3, 2])
        self.assertIsNotNone(state['generations'][0]['withdrawnAt'])
        self.assertEqual(self.state['generations'], [])  # Pure operation.
        self.assertEqual(model.decision(self.policy, state, self.start, force=True)['reason'], 'cleanup-pending')

    def test_cleanup_never_touches_unrelated_releases_or_current(self):
        state = self.populated(4)
        releases = [{'id': g['releaseId'], 'tag_name': g['tag'], 'draft': False} for g in state['generations']]
        releases.append({'id': 500, 'tag_name': '0.5.2-Sophon', 'draft': False})
        self.assertEqual(model.cleanup_plan(self.policy, state, releases, self.start + dt.timedelta(hours=47)), [])
        plan = model.cleanup_plan(self.policy, state, releases, self.start + dt.timedelta(hours=48))
        self.assertEqual(plan, [{'releaseId': 101, 'tag': 'office-f44-x86_64-g1', 'tagCommit': 'f' * 40, 'releaseExists': True}])

    def test_release_identity_drift_fails_closed(self):
        state = self.populated(4)
        with self.assertRaises(ValueError):
            model.cleanup_plan(self.policy, state, [{'id': 101, 'tag_name': 'OS-release'}], self.start + dt.timedelta(days=3))

    def test_interrupted_delete_can_resume_with_release_already_gone(self):
        plan = model.cleanup_plan(self.policy, self.populated(4), [], self.start + dt.timedelta(days=3))
        self.assertEqual(plan[0]['releaseExists'], False)

    def test_duplicate_and_mutated_generations_rejected(self):
        state = self.populated(3)
        self.assertEqual(model.promote(self.policy, state, generation(3), 3, self.start), state)
        with self.assertRaises(ValueError):
            model.promote(self.policy, state, dict(generation(3), inventorySha256='a' * 64), 3, self.start)
        with self.assertRaises(ValueError):
            model.promote(self.policy, self.populated(4), generation(5), 5, self.start)
        bad = copy.deepcopy(state); bad['generations'][0]['tag'] = '0.5.2-Sophon'
        with self.assertRaises(ValueError): model.validate_state(bad)


class CompatibilityTests(unittest.TestCase):
    PATCH = b'diff --git a/src/main.rs b/src/main.rs\nindex 111..222 100644\n--- a/src/main.rs\n+++ b/src/main.rs\n@@ -1,3 +1,3 @@\n fn main() {\n-    title("ArtCraft");\n+    title("GREYWARD");\n }\n'
    ORIGINAL = b'fn main() {\n    title("ArtCraft");\n}\n'

    def test_unchanged_and_offset_only_context_remain_exact(self):
        for prefix in (b'', b'// new upstream feature elsewhere\n'):
            result = model.exact_adaptations(self.PATCH, {'src/main.rs': prefix + self.ORIGINAL})
            self.assertEqual(result['src/main.rs'], prefix + self.ORIGINAL.replace(b'ArtCraft', b'GREYWARD'))

    def test_changed_or_ambiguous_context_blocks(self):
        for value in (self.ORIGINAL.replace(b'ArtCraft', b'NewTitle'), self.ORIGINAL * 2):
            with self.assertRaises(ValueError): model.exact_adaptations(self.PATCH, {'src/main.rs': value})

    def test_inventory_and_path_drift_block(self):
        with self.assertRaises(ValueError): model.exact_adaptations(self.PATCH, {'other.rs': self.ORIGINAL})
        with self.assertRaises(ValueError): model.exact_adaptations(self.PATCH.replace(b'src/main.rs', b'../main.rs'), {'../main.rs': self.ORIGINAL})

    def test_binary_and_malformed_hunks_block(self):
        for patch in (self.PATCH.replace(b'@@ -1,3 +1,3', b'@@ -1,9 +1,9'), self.PATCH + b'Binary files differ\n'):
            with self.assertRaises(ValueError): model.exact_adaptations(patch, {'src/main.rs': self.ORIGINAL})

    def test_real_git_apply_matches_exact_offset_transform_and_blocks_license_drift(self):
        from candidate import adapt
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); old, new = root / 'old', root / 'new'
            for tree in (old,new):
                (tree / 'src').mkdir(parents=True)
                (tree / '.github/workflows').mkdir(parents=True)
                (tree / 'src/main.rs').write_bytes(b'// prolog\n' + self.ORIGINAL + b'// epilog\n')
                (tree / 'LICENSE').write_bytes(b'MIT\n')
                (tree / 'NOTICE').write_bytes(b'Original attribution\n')
                (tree / '.github/workflows/release.yml').write_bytes(b'fonts: pinned-fonts\n')
            source = b'// added elsewhere\n// prolog\n' + self.ORIGINAL + b'// epilog\n'
            (new / 'src/main.rs').write_bytes(source)
            patchfile = root / 'reviewed.patch'; patchfile.write_bytes(self.PATCH.replace(b'@@ -1,3 +1,3', b'@@ -2,3 +2,3'))
            app = {'noticeSha256':hashlib.sha256(b'Original attribution\n').hexdigest(),
                   'fontsCommit':'pinned-fonts','adaptedInputs':{'src/main.rs':'unused'}}
            hashes = adapt(app,old,new,patchfile)
            self.assertEqual(hashes['src/main.rs'],hashlib.sha256(source).hexdigest())
            self.assertIn(b'GREYWARD',(new/'src/main.rs').read_bytes())
            (new/'LICENSE').write_bytes(b'Different license\n')
            with self.assertRaisesRegex(ValueError,'license changed'): adapt(app,old,new,patchfile)

    def test_pristine_mime_is_compared_before_patch_not_against_installed_hash(self):
        from candidate import adapt
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);old,new=root/'old',root/'new'
            for tree in (old,new):
                (tree/'src').mkdir(parents=True);(tree/'.github/workflows').mkdir(parents=True)
                (tree/'src/main.rs').write_bytes(self.ORIGINAL);(tree/'LICENSE').write_bytes(b'MIT\n')
                (tree/'NOTICE').write_bytes(b'Notice\n');(tree/'.github/workflows/release.yml').write_bytes(b'fonts: pin\n')
                (tree/'mime.xml').write_bytes(b'upstream MIME\n')
            patchfile=root/'reviewed.patch';patchfile.write_bytes(self.PATCH)
            app={'noticeSha256':hashlib.sha256(b'Notice\n').hexdigest(),'fontsCommit':'pin',
                 'adaptedInputs':{'src/main.rs':'unused'},'mimeSource':'mime.xml','mimeSha256':'patched-hash-is-different'}
            self.assertIn('src/main.rs',adapt(app,old,new,patchfile))


class SealingTests(unittest.TestCase):
    def setUp(self):
        from seal import validate_candidate
        self.validate = validate_candidate
        self.base = json.loads((ROOT / 'packaging/greyward-office/source.json').read_text())

    def test_candidate_cannot_change_trust_code_artwork_or_identity(self):
        self.validate(self.base, copy.deepcopy(self.base))
        for field in ('name', 'package', 'appId', 'patchSha256', 'iconSha256', 'fontsCommit'):
            candidate = copy.deepcopy(self.base); candidate['applications'][0][field] = 'different'
            with self.subTest(field=field), self.assertRaises(ValueError): self.validate(self.base, candidate)
        candidate = copy.deepcopy(self.base); candidate['integrationSources']['extra.py'] = 'a' * 64
        with self.assertRaises(ValueError): self.validate(self.base, candidate)

    def test_equal_upstream_version_cannot_change_commit_or_inputs(self):
        for field in ('commit', 'sha256', 'cargoLockSha256'):
            candidate = copy.deepcopy(self.base); candidate['applications'][0][field] = 'b' * len(candidate['applications'][0][field])
            with self.subTest(field=field), self.assertRaises(ValueError): self.validate(self.base, candidate)

    def test_unknown_or_missing_dependency_license_requires_review(self):
        from seal import check_dependencies
        policy=json.loads((AUTOMATION/'dependency-policy.json').read_text())
        check_dependencies(policy,[{'name':'known','license':'MIT','source':'registry'}])
        for value in ('Unknown-License',None):
            with self.assertRaises(ValueError):check_dependencies(policy,[{'name':'new','license':value,'source':'registry'}])

    def test_source_archive_cannot_overwrite_comparison_tree_or_install_git_metadata(self):
        from candidate import extract_release
        import tarfile,io
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            for i,name in enumerate(('release/../old/LICENSE','release/.git/config','other/file')):
                archive=root/(str(i)+'.tar')
                with tarfile.open(archive,'w') as out:
                    info=tarfile.TarInfo(name);info.size=1;out.addfile(info,io.BytesIO(b'x'))
                with self.assertRaises(ValueError):extract_release(archive,root/('extract-'+str(i)),'release')
            self.assertFalse((root/'old').exists())

    def test_public_component_has_closed_hash_inventory_and_no_python_cache(self):
        from bootstrap import export
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / 'component'; export(ROOT, target)
            self.assertFalse(list(target.rglob('*.pyc')))
            self.assertFalse(list(target.rglob('__pycache__')))
            inventory = json.loads((target / 'component-export.json').read_text())['files']
            actual = {p.relative_to(target).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                      for p in target.rglob('*') if p.is_file() and p.name != 'component-export.json'}
            self.assertEqual({i['path']: i['sha256'] for i in inventory}, actual)
            self.assertFalse((target / '.git').exists())
            self.assertFalse((target / 'output').exists())
            self.assertFalse(json.loads((target / 'packaging/greyward-office/automation/policy.json').read_text())['enabled'])

    def test_export_excludes_unrelated_development_files_and_preserves_originals(self):
        from bootstrap import export
        from seal import recipe_files
        import shutil
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); source = root / 'source'
            for p in recipe_files(ROOT):
                target = source / p.relative_to(ROOT);target.parent.mkdir(parents=True, exist_ok=True);shutil.copyfile(p,target)
            private = source / 'packaging/greyward-office/keys/private-development.key'
            private.write_text('Deliberately fake private-development sentinel')
            extra = source / 'packaging/greyward-office/automation/unrelated-development.txt'
            extra.write_text('Keep the private original')
            output = root / 'public';export(source,output)
            self.assertTrue(private.exists());self.assertTrue(extra.exists())
            self.assertFalse((output / private.relative_to(source)).exists())
            self.assertFalse((output / extra.relative_to(source)).exists())


class InputDownloadTests(unittest.TestCase):
    def check_download(self, responses, *, mirror=True, existing=None):
        import candidate  # Loads the trusted sibling prepare module.
        import prepare
        payload = b'reviewed-input'
        item = {'url': 'https://origin.invalid/model', 'sha256': hashlib.sha256(payload).hexdigest()}
        mirrors = {(item['url'], item['sha256']): ('https://mirror.invalid/model',)} if mirror else {}
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory)
            if existing is not None: (cache / 'model').write_bytes(existing)
            with patch.dict(prepare.REVIEWED_INPUT_MIRRORS, mirrors, clear=True), patch.object(
                    prepare.urllib.request, 'urlopen', side_effect=responses) as request:
                try:
                    result = prepare.verified_file(cache, item, 'model').read_bytes()
                except (ValueError, RuntimeError) as error:
                    result = error
                self.assertFalse((cache / 'model.part').exists())
                return result, [c.args[0] for c in request.call_args_list]

    def test_original_success_does_not_contact_mirror(self):
        result, calls = self.check_download([io.BytesIO(b'reviewed-input')])
        self.assertEqual(result, b'reviewed-input')
        self.assertEqual(calls, ['https://origin.invalid/model'])

    def test_unavailable_original_uses_only_reviewed_byte_identical_mirror(self):
        result, calls = self.check_download([
            urllib.error.HTTPError('https://origin.invalid/model', 403, 'Forbidden', {}, None),
            io.BytesIO(b'reviewed-input')])
        self.assertEqual(result, b'reviewed-input')
        self.assertEqual(calls, ['https://origin.invalid/model', 'https://mirror.invalid/model'])

    def test_bad_primary_digest_never_falls_back(self):
        result, calls = self.check_download([io.BytesIO(b'wrong')])
        self.assertIsInstance(result, ValueError)
        self.assertEqual(len(calls), 1)

    def test_bad_mirror_digest_is_rejected(self):
        result, _ = self.check_download([urllib.error.URLError('Unavailable'), io.BytesIO(b'wrong')])
        self.assertIsInstance(result, ValueError)

    def test_unknown_input_never_infers_a_mirror(self):
        result, calls = self.check_download([urllib.error.URLError('Unavailable')], mirror=False)
        self.assertIsInstance(result, RuntimeError)
        self.assertEqual(calls, ['https://origin.invalid/model'])

    def test_corrupt_cache_is_rejected_without_network(self):
        result, calls = self.check_download([], existing=b'wrong')
        self.assertIsInstance(result, ValueError)
        self.assertEqual(calls, [])


class TransportTests(unittest.TestCase):
    def test_authenticated_api_redirect_is_not_followed(self):
        from github import NoRedirect
        with self.assertRaises(ValueError):
            NoRedirect().redirect_request(None, None, 302, 'Found', {}, 'https://other.invalid/')

    def test_download_cannot_escape_owned_releases(self):
        from github import GitHub
        api = GitHub(token='deliberately-not-a-credential')
        for url in ('https://github.com/other/repo/releases/download/a/file',
                    'https://github.com/MANTIS-devlab/GREYWARD-office-updates/releases/download/../../other'):
            with self.assertRaises(ValueError): api.download(url, Path('unused'))

    def test_generated_commit_cannot_write_workflow_or_parent_path(self):
        from github import GitHub
        api = GitHub(token='deliberately-not-a-credential')
        with patch.object(api, 'request', side_effect=[{'object': {'sha': 'a' * 40}}, {'tree': {'sha': 'b' * 40}}]) as request:
            with self.assertRaises(ValueError): api.commit({'.github/workflows/other.yml': b'bad'}, 'invalid')
        self.assertEqual(request.call_count, 2)


class OrchestrationTests(unittest.TestCase):
    def state(self):
        policy = json.loads((AUTOMATION / 'policy.json').read_text()); policy['enabled'] = True
        state = model.new_state('2026-01-01T00:00:00Z')
        now = model.instant('2026-01-20T00:00:00Z')
        for n in range(1, 5): state = model.promote(policy, state, generation(n), n, now)
        state['metadataCommit'] = 'b' * 40; state['channelVerified'] = state['current']
        return policy, state, now + dt.timedelta(hours=48)

    def test_unverified_promotion_blocks_another_build(self):
        policy, state, now = self.state(); state['channelVerified'] = None
        self.assertEqual(model.decision(policy, state, now, True)['reason'], 'channel-unverified')

    def test_duplicate_remote_ids_block_cleanup(self):
        _, state, _ = self.state(); state['generations'][0]['releaseId'] = state['generations'][1]['releaseId']
        with self.assertRaises(ValueError): model.validate_state(state)

    def test_missing_current_pointer_and_withdrawn_current_are_invalid(self):
        _,state,_=self.state()
        broken=copy.deepcopy(state);broken['current']=None
        with self.assertRaises(ValueError):model.validate_state(broken)
        state['generations'][-1]['withdrawnAt']='2026-01-01T00:00:00Z'
        with self.assertRaises(ValueError):model.validate_state(state)

    def test_moved_or_missing_tag_is_not_blindly_deleted(self):
        import release
        class Missing:
            def request(self,*args,**kwargs):return None
        release.delete_owned_tag(Missing(),'office-f44-x86_64-g1','f'*40)
        class Moved:
            def request(self,path,method='GET',**kwargs):
                if method!='GET':raise AssertionError('No deletion of moved work')
                return {'ref':'refs/tags/office-f44-x86_64-g1','object':{'type':'commit','sha':'a'*40}}
        with self.assertRaises(ValueError):release.delete_owned_tag(Moved(),'office-f44-x86_64-g1','f'*40)

    def test_changed_pointer_and_disabled_policy_cannot_delete(self):
        import release
        policy, state, now = self.state()
        class Fake:
            def releases(self): return [{'id':101, 'tag_name':'office-f44-x86_64-g1', 'draft':False}]
            def content(self, path): return b'changed pointer'
            def request(self, path, method='GET', **kwargs):
                if method=='GET' and path.startswith('/releases/'):
                    n=int(path.rsplit('/',1)[1]);return {'id':n,'tag_name':'office-f44-x86_64-g'+str(n-100),'draft':False}
                raise AssertionError('No deletion permitted')
        with self.assertRaises(ValueError): release.clean(Fake(), policy, state, now, True)
        policy['enabled'] = False
        with self.assertRaises(ValueError): release.clean(Fake(), policy, state, now, True)

    def test_cleanup_applies_exact_owned_ids_and_commits_after_deletion(self):
        import release
        policy, state, now = self.state(); events=[]
        class Fake:
            def releases(self): return [{'id':101, 'tag_name':'office-f44-x86_64-g1', 'draft':False}, {'id':999,'tag_name':'OS-0.5.2'}]
            def content(self, path): return (release.snapshot_url(state['metadataCommit'],state['current'])+'\n').encode()
            def request(self, path, method='GET', **kwargs):
                if method=='GET' and path.startswith('/releases/'):
                    n=int(path.rsplit('/',1)[1]);return {'id':n,'tag_name':'office-f44-x86_64-g'+str(n-100),'draft':False}
                events.append((method,path))
                if method=='GET':return {'ref':'refs/tags/office-f44-x86_64-g1','object':{'type':'commit','sha':'f'*40}}
            def commit(self, files, message): events.append(('COMMIT',json.loads(files[release.STATE])))
        result, _ = release.clean(Fake(), policy, state, now, True)
        self.assertEqual(events[:4], [('GET','/git/ref/tags/office-f44-x86_64-g1'),('DELETE','/releases/101'),('GET','/git/ref/tags/office-f44-x86_64-g1'),('DELETE','/git/refs/tags/office-f44-x86_64-g1')])
        self.assertEqual([g['generation'] for g in result['generations']], [2,3,4])
        self.assertEqual(events[4][0], 'COMMIT')


if __name__ == '__main__': unittest.main()
