import datetime as dt
import importlib.util
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('office_upstream', ROOT / 'packaging/greyward-office/upstream.py')
upstream = importlib.util.module_from_spec(spec)
spec.loader.exec_module(upstream)
NOW = dt.datetime(2026, 10, 27, tzinfo=dt.timezone.utc)


def release(version='0.11.0', repository='wordcraft', **extra):
    return {'draft': False, 'prerelease': False, 'tag_name': 'v' + version,
            'published_at': '2026-10-26T12:00:00Z',
            'html_url': f'https://github.com/storytold/{repository}/releases/tag/v{version}', **extra}


class StableOfficeReleases(unittest.TestCase):
    def test_semantic_latest_ignores_preview_and_out_of_order_feed(self):
        values = [release('0.9.9'), release('0.12.0', prerelease=True), release('0.11.0'), release('0.10.9')]
        actual = upstream.latest_release('wordcraft', lambda *_: values, NOW)
        self.assertEqual(actual['version'], '0.11.0')

    def test_unknown_ownership_and_unpublished_or_future_entries_are_rejected(self):
        for change in ({'draft': True}, {'prerelease': True}, {'tag_name': 'v0.12.0-beta'},
                       {'html_url': 'https://example.invalid/download'}, {'published_at': None},
                       {'published_at': '2027-01-01T00:00:00Z'}, {'published_at': '2026-10-26'}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                upstream.stable_release(release(**change), 'wordcraft', NOW)

    def test_network_failure_does_not_become_up_to_date(self):
        manifest = {'applications': [{'slug':'writer', 'name':'Writer', 'version':'0.10.0', 'upstream':'wordcraft'}]}
        def unavailable(*_): raise OSError('offline')
        actual = upstream.discover(manifest, unavailable, NOW)
        self.assertEqual(actual['unavailable'], 1)
        self.assertEqual(actual['applications'][0]['state'], 'UNAVAILABLE')
        self.assertNotIn('version', actual['applications'][0])

    def test_discovery_reports_an_update_without_mutating_the_reviewed_pin(self):
        app = {'slug':'writer', 'name':'Writer', 'version':'0.10.0', 'upstream':'wordcraft'}
        actual = upstream.discover({'applications':[app]}, lambda *_: [release()], NOW)
        self.assertEqual(actual['available'], 1)
        self.assertEqual(app['version'], '0.10.0')
        self.assertEqual(actual['applications'][0]['state'], 'UPDATE_AVAILABLE')

    def test_stale_feed_is_not_reported_as_current(self):
        app = {'slug': 'writer', 'name': 'Writer', 'version': '0.11.0', 'upstream': 'wordcraft'}
        actual = upstream.discover({'applications': [app]}, lambda *_: [release('0.10.0')], NOW)
        self.assertEqual(actual['unavailable'], 1)
        self.assertEqual(actual['applications'][0]['state'], 'UNAVAILABLE')

    def test_malformed_newer_stable_entry_cannot_hide_behind_an_older_valid_release(self):
        with self.assertRaisesRegex(ValueError, 'expected official repository'):
            upstream.latest_release('wordcraft', lambda *_: [release(), release('0.12.0', html_url='https://example.invalid/')], NOW)


if __name__ == '__main__':
    unittest.main()
