import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from backend import db, rag, trend_service
from backend.query_utils import rule_query_analysis

NOW = datetime(2026, 9, 9, tzinfo=timezone.utc)


def row(identifier, title, source, days=1, category='music', duplicate=None):
    return {
        'article_id': identifier,
        'title': title,
        'content': title,
        'category': category,
        'source_name': source,
        'url': f'https://example.com/{identifier}',
        'url_hash': duplicate or f'hash-{identifier}',
        'published_at': NOW - timedelta(days=days),
        'collected_at': NOW,
    }


class TrendRankingTests(unittest.TestCase):
    def test_trend_intents(self):
        for question in [
            '요즘 누가 유명해?',
            '최근 많이 언급되는 연예인은 누구야?',
            '요즘 뜨는 아이돌 누구야?',
            '최근 화제 인물 알려줘',
            '요즘 많이 언급되는 그룹 알려줘',
        ]:
            self.assertEqual(rule_query_analysis(question).intent, 'trend_ranking', question)

    def test_periods(self):
        self.assertEqual(trend_service.period_days('recent'), 7)
        self.assertEqual(trend_service.period_days('month'), 30)
        self.assertEqual(trend_service.period_start('year', NOW), datetime(2026, 1, 1, tzinfo=timezone.utc))

    def test_source_diversity_and_recency(self):
        rows = [
            row(1, 'A그룹 컴백 소식', 'A', 1), row(2, 'A그룹 방송 출연', 'A', 1),
            row(3, 'B그룹 컴백 소식', 'B1', 1), row(4, 'B그룹 방송 출연', 'B2', 1),
            row(5, 'B그룹 행사 소식', 'B3', 2),
        ]
        results = trend_service.aggregate_rows(rows, NOW, top_k=5)
        self.assertEqual([item.name for item in results[:2]], ['B그룹', 'A그룹'])
        self.assertEqual(results[0].source_count, 3)

    def test_duplicate_articles_do_not_inflate_counts(self):
        rows = [row(1, 'A그룹 컴백', 'A', duplicate='same'), row(2, 'A그룹 컴백', 'B', duplicate='same')]
        results = trend_service.aggregate_rows(rows, NOW, top_k=5)
        self.assertEqual(results[0].mention_count, 1)

    def test_no_recent_articles(self):
        with patch.object(db, 'fetch_all', return_value=[]):
            results, scanned = trend_service.aggregate('recent', now=NOW)
        self.assertEqual(results, [])
        self.assertEqual(scanned, 0)

    def test_trend_branch_keeps_api_shape_and_uses_no_vector_search(self):
        items = [trend_service.TrendItem('A그룹', 3, 2, NOW, .9, (row(1, 'A그룹 컴백', 'A'),))]
        with patch.object(rag.trend_service, 'aggregate', return_value=(items, 3)), \
                patch.object(rag, 'generate_trend_answer', return_value='최근 수집 기사 기준으로 A그룹이 3건으로 집계됐습니다.[1]'), \
                patch.object(rag, '_search') as search, \
                patch.object(db, 'execute'):
            result = rag.answer_question('요즘 누가 유명해?', 5)
        search.assert_not_called()
        self.assertEqual(result.domain, 'ent_culture')
        self.assertEqual(len(result.sources), 1)
        self.assertIn('A그룹', result.answer)


if __name__ == '__main__':
    unittest.main()
