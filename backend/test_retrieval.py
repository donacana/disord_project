import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from fastapi.testclient import TestClient

from backend import db, main, rag, retrieval
from backend.query_utils import extract_keywords

NOW = datetime(2026, 9, 9, tzinfo=timezone.utc)


def article(identifier, title, similarity=0.6, content='', days=1):
    return dict(article_id=identifier, title=title, content=content, summary=None,
                url=f'https://example.com/{identifier}', source_name='test', category='movie',
                published_at=NOW - timedelta(days=days), collected_at=NOW,
                distance=1 - similarity)


class RetrievalTests(unittest.TestCase):
    def test_keywords(self):
        for question, expected in [
            ('아이브 요즘 활동 뭐 했어?', ['아이브']),
            ('장원영 최근 광고 뭐 찍었어?', ['장원영', '광고']),
            ('최근 영화 파묘 관련 소식', ['파묘']),
            ('최근 영화 관련 주요 이슈 알려줘', ['영화']),
            ('아이브의 활동 알려줘', ['아이브']),
            ('최근 소식 알려줘', []),
        ]:
            self.assertEqual(extract_keywords(question), expected)

    def test_title_body_unrelated_order(self):
        rows = [article(1, 'ITZY 소식', .7), article(2, '공연 소식', .6, '아이브'),
                article(3, '아이브 월드투어', .6)]
        ranked = retrieval.rank_candidates(rows, '아이브 요즘 활동 뭐 했어?', 5, now=NOW)
        self.assertEqual([r['article_id'] for r in ranked], [3, 2, 1])
        self.assertNotIn('final_score', rows[0])

    def test_threshold_candidates_and_no_padding(self):
        rows = [article(1, '아이브', .29), article(2, '무관 기사', .6)]
        with patch.object(db, 'fetch_all', return_value=rows) as fetch:
            result = retrieval.search([0.1], '아이브', 5, .3)
        self.assertEqual(fetch.call_args.args[1][-1], 20)
        self.assertEqual([r['article_id'] for r in result], [2])
        self.assertEqual(retrieval.rank_candidates(rows, '아이브', 5, .9), [])

    def test_recency_and_no_keyword_fallback(self):
        for days, expected in [(0, 1), (7, 1), (8, .7), (30, .7), (31, .3), (90, .3), (91, 0)]:
            self.assertEqual(retrieval.recency_score(NOW - timedelta(days=days), NOW), expected)
        self.assertEqual(retrieval.recency_score(None, NOW), 0)
        self.assertEqual(retrieval.recency_score('invalid', NOW), 0)
        self.assertEqual(retrieval.recency_score(NOW.replace(tzinfo=None), NOW), 1)
        rows = [article(1, '영화 개봉', days=100), article(2, '영화 개봉')]
        self.assertEqual(retrieval.rank_candidates(rows, '최근 영화 이슈', 1, now=NOW)[0]['article_id'], 2)
        self.assertEqual(retrieval.rank_candidates(rows, '영화', 1, now=NOW)[0]['article_id'], 1)
        self.assertEqual(len(retrieval.rank_candidates(rows, '알려줘', 5, now=NOW)), 2)

    def test_debug_opt_in(self):
        with patch.dict('os.environ', {'RAG_DEBUG': 'false'}), patch.object(retrieval.logger, 'warning') as log:
            retrieval.rank_candidates([article(1, '아이브')], '아이브', 1)
            log.assert_not_called()
        with patch.dict('os.environ', {'RAG_DEBUG': 'true'}), patch.object(retrieval.logger, 'warning') as log:
            retrieval.rank_candidates([article(1, '아이브')], '아이브', 1)
            log.assert_called_once()

    def test_api_contract_context_and_logging(self):
        client = TestClient(main.app)
        rows = [article(1, '아이브 월드투어'), article(2, 'ITZY 소식')]
        with patch.object(db, 'fetch_one', return_value={'ok': 1}):
            self.assertEqual(client.get('/health').json()['status'], 'ok')
        stats = dict(total_articles=2, total_embeddings=2, last_collected_at=None, source_count=1)
        with patch.object(db, 'fetch_one', return_value=stats):
            self.assertEqual(client.get('/stats').json(), stats)
        with patch.object(rag, 'embed_question', return_value=[0.1]), \
                patch.object(db, 'fetch_all', return_value=rows), \
                patch.object(rag, 'generate_answer', return_value='아이브 소식 [1]') as generate, \
                patch.object(db, 'execute') as log:
            response = client.post('/ask', json={'question': '아이브', 'top_k': 1})
            self.assertEqual(response.status_code, 200)
            payload = response.json()
            self.assertEqual(set(payload), {'answer', 'sources', 'domain'})
            self.assertEqual(payload['domain'], 'ent_culture')
            self.assertEqual(set(payload['sources'][0]), {'title', 'url', 'collected_at'})
            self.assertNotIn('ITZY', generate.call_args.args[1])
            self.assertIn('[1]\n제목:', generate.call_args.args[1])
            log.assert_called_once()
        with patch.object(rag, '_search', return_value=[]), \
                patch.object(rag, 'generate_answer') as generate, patch.object(db, 'execute') as log:
            self.assertEqual(client.post('/ask', json={'question': '없음'}).json()['sources'], [])
            generate.assert_not_called()
            log.assert_called_once()
        self.assertEqual(client.post('/ask', json={'question': ' ', 'top_k': 1}).status_code, 422)
        with patch.object(db, 'fetch_one', side_effect=db.DatabaseError('unavailable')):
            self.assertEqual(client.get('/health').status_code, 503)
            self.assertEqual(client.get('/stats').status_code, 503)


if __name__ == '__main__':
    unittest.main()
