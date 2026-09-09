import contextlib
import io
import unittest
from datetime import datetime, timezone
from unittest.mock import Mock, patch

import psycopg
import requests
from collector import sources_naver as naver

NOW = datetime(2026, 9, 9, tzinfo=timezone.utc)


def item(number=1, date='Wed, 09 Sep 2026 00:00:00 +0000'):
    return dict(title=f'기사 {number}', originallink=f'https://example.com/{number}', pubDate=date)


class Connection:
    autocommit = True

    def __init__(self):
        self.rows = []
        self.commits = 0
        self.fail = False
        self.last = None

    def cursor(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def execute(self, query, params):
        self.last = query
        if 'INSERT' in query:
            if self.fail:
                raise psycopg.IntegrityError('test insert failed')
            self.rows.append(params)

    def fetchone(self):
        return (1,) if self.last and 'INSERT' in self.last else None

    @contextlib.contextmanager
    def transaction(self):
        previous = self.rows[:]
        try:
            yield
        except Exception:
            self.rows = previous
            raise
        else:
            self.commits += 1


class NaverTests(unittest.TestCase):
    def setUp(self):
        self.output = contextlib.redirect_stdout(io.StringIO())
        self.output.__enter__()
        self.addCleanup(self.output.__exit__, None, None, None)

    def test_year_boundaries(self):
        self.assertIsNone(naver.parse_pub_date('bad'))
        self.assertFalse(naver.is_2026_article(None))
        self.assertFalse(naver.is_2026_article(datetime(2027, 1, 1, tzinfo=timezone.utc)))
        date = naver.parse_pub_date('Thu, 01 Jan 2026 09:00:00 +0900')
        self.assertEqual(date, naver.START_DATE)

    def test_page_params_and_invalid_payload(self):
        response = Mock()
        response.json.return_value = {'items': []}
        with patch.object(naver.requests, 'get', return_value=response) as get:
            naver.fetch_page('영화', 901)
        self.assertEqual(get.call_args.kwargs['params'], dict(query='영화', display=100, start=901, sort='date'))
        response.json.return_value = {'items': 'invalid'}
        with patch.object(naver.requests, 'get', return_value=response):
            with self.assertRaises(ValueError):
                naver.fetch_page('영화', 1)
        with self.assertRaises(ValueError):
            naver.fetch_page('영화', 1000, 100)

    def test_maximum_ten_pages_and_1000_candidates(self):
        future = item(date='Fri, 01 Jan 2027 00:00:00 +0000')
        with patch.object(naver, 'fetch_page', return_value=[future] * 100) as fetch, \
                patch.object(naver, 'extract_content') as extract:
            result = naver.collect_query(Connection(), 1, '영화', 'movie', now=NOW)
        self.assertEqual([call.args[1] for call in fetch.call_args_list], list(range(1, 902, 100)))
        self.assertEqual(result['api_calls'], 10)
        self.assertEqual(result['searched'], 1000)
        self.assertEqual(result['saved'], 0)
        extract.assert_not_called()

    def test_date_filter_and_older_page_stop(self):
        pages = [[item(), item(2, 'Wed, 31 Dec 2025 00:00:00 +0000')],
                 [item(3, 'Tue, 30 Dec 2025 00:00:00 +0000')] * 2]
        with patch.object(naver, 'fetch_page', side_effect=pages) as fetch, \
                patch.object(naver, 'extract_content', return_value='본문' * 200):
            result = naver.collect_query(Connection(), 1, '영화', 'movie', display=2, now=NOW)
        self.assertEqual(fetch.call_count, 2)
        self.assertEqual(result['saved'], 1)
        self.assertEqual(result['stop_reason'], 'older_than_start')

    def test_invalid_dates_do_not_prove_old_page(self):
        pages = [[item(date='bad'), item(2, 'Wed, 31 Dec 2025 00:00:00 +0000')], []]
        with patch.object(naver, 'fetch_page', side_effect=pages):
            result = naver.collect_query(Connection(), 1, '영화', 'movie', display=2, now=NOW)
        self.assertEqual(result['api_calls'], 2)
        self.assertEqual(result['date_invalid'], 1)

    def test_failed_call_counted_and_quota_stops_run(self):
        response = Mock(status_code=429)
        error = requests.HTTPError('quota', response=response)
        with patch.object(naver, 'fetch_page', side_effect=error):
            result = naver.collect_query(Connection(), 1, '영화', 'movie', now=NOW)
        self.assertEqual(result['api_calls'], 1)
        self.assertEqual(result['errors'], 1)
        self.assertTrue(result['fatal_api_error'])
        with patch.object(naver, 'validate_environment'), \
                patch.object(naver.psycopg, 'connect', return_value=Connection()), \
                patch.object(naver, 'get_source_id', return_value=1), \
                patch.object(naver, 'collect_query', return_value=result) as collect:
            self.assertEqual(naver.main(), 1)
        collect.assert_called_once()

    def test_cross_query_dedup_and_extraction_failure_retry(self):
        urls, contents = set(), set()
        conn = Connection()
        with patch.object(naver, 'fetch_page', return_value=[item()]), \
                patch.object(naver, 'extract_content', side_effect=[None, '본문' * 200]) as extract:
            first = naver.collect_query(conn, 1, '영화', 'movie', seen_url_hashes=urls, seen_content_hashes=contents, now=NOW)
            second = naver.collect_query(conn, 1, '신작', 'movie', seen_url_hashes=urls, seen_content_hashes=contents, now=NOW)
            third = naver.collect_query(conn, 1, '개봉', 'movie', seen_url_hashes=urls, seen_content_hashes=contents, now=NOW)
        self.assertEqual(first['content_failed'], 1)
        self.assertEqual(second['saved'], 1)
        self.assertEqual(third['duplicate'], 1)
        self.assertEqual(extract.call_count, 2)

    def test_same_body_different_urls(self):
        with patch.object(naver, 'fetch_page', return_value=[item(), item(2)]), \
                patch.object(naver, 'extract_content', return_value='본문' * 200):
            result = naver.collect_query(Connection(), 1, '영화', 'movie', now=NOW)
        self.assertEqual(result['saved'], 1)
        self.assertEqual(result['duplicate'], 1)

    def test_article_failure_does_not_rollback_earlier_save(self):
        conn = Connection()
        data = dict(category='movie', title='제목', content='본문', url='url', url_hash='hash',
                    content_hash='content', source_name='source', published_at=NOW)
        self.assertTrue(naver.save_article(conn, data, 1))
        conn.fail = True
        with self.assertRaises(psycopg.IntegrityError):
            naver.save_article(conn, data, 1)
        self.assertEqual(len(conn.rows), 1)
        self.assertEqual(conn.commits, 1)
        conn.autocommit = False
        with self.assertRaises(ValueError):
            naver.collect_query(conn, 1, '영화', 'movie')

    def test_search_query_count_and_url_normalization(self):
        self.assertEqual(len(naver.NAVER_SEARCH_QUERIES), 44)
        self.assertEqual(naver.normalize_url('https://example.com/a?id=&utm_source=x#section'), 'https://example.com/a?id=')


if __name__ == '__main__':
    unittest.main()
