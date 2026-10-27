import copy
import datetime as dt
import importlib.util
import json
from pathlib import Path
import sys
import unittest

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
            'publishedAt': when, 'inventorySha256': str(number % 10) * 64}


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
        self.assertEqual(plan, [{'releaseId': 101, 'tag': 'office-f44-x86_64-g1', 'releaseExists': True}])

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


if __name__ == '__main__': unittest.main()
