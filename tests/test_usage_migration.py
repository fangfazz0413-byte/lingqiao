import json
import os
from pathlib import Path
import tempfile
import unittest
import datetime
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
import sys
sys.path.insert(0, str(ROOT / 'app'))
import usage_backend
import usage_collector


class UsageMigrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cache = Path(self.tmp.name) / 'LingqiaoUsage' / 'cache.json'
        self.local = {
            'days': ['2026-10-01'], 'daily': [[0, 0, 0, 0, 0]],
            'months': ['2026-10'], 'sources': [], 'month': '2026-10',
            'today': {'day': '2026-10-01', 'tokens': 0, 'calls': 0},
        }

    def tearDown(self):
        self.tmp.cleanup()

    def payload(self):
        return {'at': 1234, 'quota_at': 1234, 'local_at': 1234,
                'quota': [], 'local': self.local}

    def test_snapshot_writes_private_injected_path_without_legacy_cli(self):
        with patch.object(usage_collector, 'all_quota', return_value=[]), \
             patch.object(usage_collector, 'collect_local', return_value=self.local):
            out = usage_collector.snapshot(force=True, cache_file=str(self.cache))
        self.assertEqual(out['local']['month'], '2026-10')
        self.assertTrue(self.cache.is_file())
        if os.name != 'nt':  # Windows 没有这种权限位，靠用户目录的访问控制
            self.assertEqual(self.cache.stat().st_mode & 0o777, 0o600)
        if os.name != 'nt':  # Windows 没有这种权限位，靠用户目录的访问控制
            self.assertEqual(self.cache.parent.stat().st_mode & 0o777, 0o700)

    def test_backend_refresh_never_invokes_cli(self):
        with patch('usage_backend.snapshot', return_value=self.payload()) as collect:
            out = usage_backend.refresh_snapshot(self.cache, force=True)
        collect.assert_called_once_with(force=True, cache_file=str(self.cache))
        self.assertEqual(out['local']['month'], '2026-10')

    def test_migrate_validated_legacy_cache_is_atomic_and_non_destructive(self):
        legacy = Path(self.tmp.name) / 'old-cache.json'
        legacy.write_text(json.dumps(self.payload()), encoding='utf-8')
        result = usage_backend.migrate_legacy_cache(legacy, self.cache)
        self.assertTrue(result['migrated'])
        self.assertTrue(legacy.exists())
        self.assertEqual(usage_backend.read_snapshot(self.cache)['local']['month'], '2026-10')
        if os.name != 'nt':  # Windows 没有这种权限位，靠用户目录的访问控制
            self.assertEqual(self.cache.stat().st_mode & 0o777, 0o600)

    def test_backend_reads_only_valid_schema(self):
        self.cache.parent.mkdir(parents=True)
        self.cache.write_text(json.dumps(self.payload()), encoding='utf-8')
        result = usage_backend.read_snapshot(self.cache)
        self.assertNotIn('secret-value', json.dumps(result, ensure_ascii=False).lower())
        self.cache.write_text(json.dumps({'quota': []}), encoding='utf-8')
        with self.assertRaises(ValueError):
            usage_backend.read_snapshot(self.cache)

    def test_provider_parsers_preserve_three_quota_families(self):
        glm, _ = usage_collector.parse_glm({'data': {'limits': [
            {'unit': 3, 'number': 5, 'percentage': 12, 'currentValue': 1, 'usage': 8}
        ]}})
        kimi, _ = usage_collector.parse_kimi({'usages': {
            'limit_5h': {'used_ratio': .25}, 'limit_7d': {'used_ratio': .4}
        }, 'usage': {'used': 4, 'limit': 10}})
        mini, _ = usage_collector.parse_minimax({'model_remains': [
            {'model_name': 'Mini', 'current_interval_remaining_percent': 80,
             'current_interval_status': 1, 'current_weekly_remaining_percent': 60,
             'current_weekly_status': 1, 'end_time': 1000, 'weekly_end_time': 1000}
        ]})
        self.assertEqual([x['window'] for x in glm], ['5h'])
        self.assertEqual([x['window'] for x in kimi], ['5h', '7d'])
        self.assertEqual([x['window'] for x in mini], ['5h', '7d'])

    def test_provider_status_contains_no_secret_values(self):
        with patch.object(usage_collector, 'kc_configured', return_value=False):
            status = usage_backend.get_provider_status()
        self.assertEqual({x['name'] for x in status}, {'glm', 'kimi', 'minimax'})
        self.assertTrue(all('configured' in x and 'key' not in x for x in status))

    def test_key_save_returns_labels_only(self):
        with patch.object(usage_collector, 'kc_set', return_value=(True, '')):
            result = usage_backend.set_provider_keys({'glm': 'secret-value'})
        self.assertEqual(result['ok'], ['智谱 GLM'])
        self.assertNotIn('secret-value', json.dumps(result))


    def test_failed_cache_replace_preserves_last_good_snapshot(self):
        self.cache.parent.mkdir(parents=True)
        old = json.dumps(self.payload()).encode()
        self.cache.write_bytes(old)
        with patch.object(usage_collector, 'all_quota', return_value=[]), \
             patch.object(usage_collector, 'collect_local', return_value=self.local), \
             patch.object(usage_collector.os, 'replace', side_effect=PermissionError('private sentinel')):
            with self.assertRaises(PermissionError):
                usage_collector.snapshot(force=True, cache_file=str(self.cache))
        self.assertEqual(self.cache.read_bytes(), old)
        self.assertEqual(list(self.cache.parent.glob('.usage-*.tmp')), [])

    def test_cross_day_refreshes_local_even_when_timestamp_is_fresh(self):
        now = time.time()
        old_local = {**self.local, 'version': usage_collector.LOCAL_VERSION,
                     'month': datetime.date.today().strftime('%Y-%m'),
                     'today': {'day': (datetime.date.today()-datetime.timedelta(days=1)).isoformat()}}
        self.cache.parent.mkdir(parents=True)
        self.cache.write_text(json.dumps({'at': now, 'quota_at': now, 'local_at': now,
                                         'quota': [], 'local': old_local}))
        with patch.object(usage_collector, 'all_quota', return_value=[]) as quotas, \
             patch.object(usage_collector, 'collect_local', return_value=self.local) as collect:
            usage_collector.snapshot(force=False, cache_file=str(self.cache))
        collect.assert_called_once(); quotas.assert_not_called()

    def test_fresh_same_day_cache_avoids_queries_and_writes(self):
        now = time.time()
        today = datetime.date.today().isoformat()
        local = {**self.local, 'version': usage_collector.LOCAL_VERSION,
                 'month': today[:7], 'today': {'day': today}}
        self.cache.parent.mkdir(parents=True)
        self.cache.write_text(json.dumps({'at': now, 'quota_at': now, 'local_at': now,
                                         'quota': [], 'local': local}))
        with patch.object(usage_collector, 'all_quota') as quotas, \
             patch.object(usage_collector, 'collect_local') as collect, \
             patch.object(usage_collector, 'atomic_private_json') as write:
            usage_collector.snapshot(force=False, cache_file=str(self.cache))
        quotas.assert_not_called(); collect.assert_not_called(); write.assert_not_called()

    def test_key_validation_is_all_or_nothing_and_never_echoes_secret(self):
        bad_values = ({'glm':'valid-secret', 'kimi':None}, {'alien':'private-sentinel'},
                      {'glm':'private-sentinel\n'}, {'glm':''}, [])
        for values in bad_values:
            with self.subTest(values_type=type(values).__name__), \
                 patch.object(usage_collector, 'kc_set') as save:
                with self.assertRaises(ValueError) as caught:
                    usage_backend.set_provider_keys(values)
                self.assertNotIn('private-sentinel', str(caught.exception))
                self.assertNotIn('valid-secret', str(caught.exception))
                save.assert_not_called()

    def test_key_adapter_error_response_is_redacted(self):
        with patch.object(usage_collector, 'kc_set', side_effect=RuntimeError('private-sentinel')):
            result = usage_backend.set_provider_keys({'glm':'fake-test-secret'})
        self.assertEqual(result['ok'], [])
        self.assertEqual(result['fail'][0]['label'], '智谱 GLM')
        self.assertNotIn('sentinel', json.dumps(result))
        self.assertNotIn('fake-test-secret', json.dumps(result))

    def test_backend_failure_is_redacted(self):
        with patch.object(usage_backend, 'snapshot', side_effect=RuntimeError('private-sentinel')):
            with self.assertRaises(RuntimeError) as caught:
                usage_backend.refresh_snapshot(self.cache, force=True)
        self.assertNotIn('private-sentinel', str(caught.exception))

    def test_ninety_day_bucket_excludes_older_year_data(self):
        days = usage_collector.day_list(365)
        src = {'label':'fixture', 'note':'fixture', 'records':[],
               'daily':{d:[1,1,1,0,0] for d in days}, 'hourly':{}, 'day_models':{}}
        with patch.object(usage_collector, 'SOURCES', [lambda:src]):
            local = usage_collector.collect_local()
        self.assertEqual(local['stat_90d']['tokens'], 90)
        self.assertEqual(local['stat_7d']['tokens'], 7)
        self.assertEqual(len(local['daily']), 365)

    def test_corrupt_target_is_not_overwritten_during_migration(self):
        legacy = Path(self.tmp.name)/'old-cache.json'
        legacy.write_text(json.dumps(self.payload()))
        self.cache.parent.mkdir(parents=True)
        self.cache.write_text('broken-target')
        with self.assertRaises(ValueError):
            usage_backend.migrate_legacy_cache(legacy, self.cache)
        self.assertEqual(self.cache.read_text(), 'broken-target')


    def test_partial_local_collection_does_not_replace_snapshot(self):
        self.cache.parent.mkdir(parents=True)
        old = json.dumps(self.payload()).encode()
        self.cache.write_bytes(old)
        with patch.object(usage_collector, 'all_quota', return_value=[]), \
             patch.object(usage_collector, 'collect_local', return_value={**self.local, 'errors':[{'label':'fixture'}]}):
            with self.assertRaises(RuntimeError):
                usage_collector.snapshot(force=True, cache_file=str(self.cache))
        self.assertEqual(self.cache.read_bytes(), old)

    def test_unreadable_nonempty_wal_refuses_immutable_fallback(self):
        db = Path(self.tmp.name)/'usage.db'
        Path(str(db)+'-wal').write_bytes(b'not-empty')
        with patch.object(usage_collector.sqlite3, 'connect', side_effect=usage_collector.sqlite3.OperationalError('fixture')) as connect:
            with self.assertRaises(usage_collector.sqlite3.OperationalError):
                usage_collector._ro(str(db))
        self.assertEqual(connect.call_count, 1)


if __name__ == '__main__':
    unittest.main()
