import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from backend import config, diagnostics, entity_cache, trend_service
from backend.openai_client import OpenAIServiceError


class EntityCacheTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        cache = patch.object(config, 'TREND_ENTITY_CACHE_PATH', str(Path(directory.name) / 'cache.sqlite3'))
        cache.start()
        self.addCleanup(cache.stop)
        diagnostics.begin('cache test')

    def test_inserted_article_does_not_invalidate_existing_articles(self):
        with patch.object(trend_service, '_extract_batch', return_value=(('A그룹', 'idol_or_group', 0),)) as extract:
            first = trend_service._article_mentions(['그룹 A그룹 활동', '문화 소식'])
        self.assertEqual(first, [[('A그룹', 'idol_or_group')], []])
        with patch.object(trend_service, '_extract_batch', return_value=(('B그룹', 'idol_or_group', 0),)) as extract:
            second = trend_service._article_mentions(['그룹 B그룹 활동', '그룹 A그룹 활동', '문화 소식'])
        extract.assert_called_once_with(('그룹 B그룹 활동',))
        self.assertEqual(second[1:], first)
        self.assertEqual(diagnostics.snapshot()['entity_cache_hits'], 2)
        self.assertEqual(diagnostics.snapshot()['entity_cache_misses'], 1)

    def test_content_and_model_changes_invalidate_key(self):
        original = entity_cache.key('원문')
        self.assertNotEqual(original, entity_cache.key('수정 원문'))
        with patch.dict('os.environ', {'OPENAI_CHAT_MODEL': 'different-model'}):
            self.assertNotEqual(original, entity_cache.key('원문'))

    def test_successful_empty_extraction_survives_new_connection(self):
        cache_key = entity_cache.key('기사')
        entity_cache.put_many({cache_key: ([], True)})
        self.assertEqual(entity_cache.get_many([cache_key]), {cache_key: ([], True)})

    def test_expired_entries_are_retried(self):
        cache_key = entity_cache.key('기사')
        with patch.object(entity_cache.time, 'time', return_value=100), patch.object(config, 'TREND_ENTITY_CACHE_TTL_SECONDS', 1):
            entity_cache.put_many({cache_key: ([('이름', 'actor')], True)})
        with patch.object(entity_cache.time, 'time', return_value=102):
            self.assertEqual(entity_cache.get_many([cache_key]), {})

    def test_failure_backoff_retains_partial_flag_and_then_retries(self):
        with patch.object(trend_service, '_extract_batch', side_effect=OpenAIServiceError('timeout')):
            trend_service._article_mentions(['자료'])
        with patch.object(trend_service, '_extract_batch') as extract:
            trend_service._article_mentions(['자료'])
        extract.assert_not_called()
        self.assertEqual(diagnostics.snapshot()['entity_extraction_failures'], 0)
        self.assertEqual(diagnostics.snapshot()['entity_extraction_incomplete_articles'], 1)
        import time
        later = time.time() + config.TREND_ENTITY_RETRY_SECONDS + 1
        with patch.object(entity_cache.time, 'time', return_value=later), \
                patch.object(trend_service, '_extract_batch', return_value=()) as extract:
            trend_service._article_mentions(['자료'])
        extract.assert_called_once()
        self.assertEqual(diagnostics.snapshot()['entity_extraction_incomplete_articles'], 0)

    def test_unavailable_cache_does_not_break_extraction(self):
        with patch.object(config, 'TREND_ENTITY_CACHE_PATH', str(Path(config.TREND_ENTITY_CACHE_PATH).parent)), \
                patch.object(trend_service, '_extract_batch', return_value=(('A그룹', 'idol_or_group', 0),)):
            self.assertEqual(trend_service._article_mentions(['그룹 A그룹']), [[('A그룹', 'idol_or_group')]])

    def test_concurrent_requests_share_extraction(self):
        rows = [{'title': '그룹 A그룹', 'content': '그룹 A그룹 활동'}]
        with patch.object(trend_service, '_extract_batch', return_value=(('A그룹', 'idol_or_group', 0),)) as extract:
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(lambda _: trend_service._entity_rows(rows, 'idol'), range(2)))
        extract.assert_called_once()
        self.assertEqual(results[0], results[1])


if __name__ == '__main__':
    unittest.main()
