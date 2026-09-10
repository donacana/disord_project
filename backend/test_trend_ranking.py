import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from backend import db, rag, trend_service, diagnostics
from backend.answer_validator import INSUFFICIENT_ANSWER
from backend.query_utils import QueryAnalysis, rule_query_analysis

NOW = datetime(2026, 9, 9, tzinfo=timezone.utc)


def row(identifier, title, source, days=1, category='music', duplicate=None):
    return {
        'article_id': identifier,
        'title': title,
        'content': title,
        'category': category,
        'source_name': source,
        'url': f'https://{source or "unknown"}.example/{identifier}',
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
            '요즘 어떤 아이돌이 유명해?',
            '최근 활동이 많은 아이돌 알려줘',
            '최근 화제인 배우 알려줘',
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
        analysis = QueryAnalysis('요즘 누가 유명해?', '최근 화제 인물', None,
                     'trend_ranking', 'recent', ('화제',), (), .9)
        with patch.object(rag, '_query_analysis', return_value=(analysis, True)), \
            patch.object(rag.trend_service, 'aggregate', return_value=(items, 3)), \
                patch.object(rag, 'generate_trend_answer', return_value='최근 수집 기사 기준으로 A그룹이 3건으로 집계됐습니다.[1]'), \
                patch.object(rag, 'verify_answer', return_value='최근 수집 기사 기준으로 A그룹이 3건으로 집계됐습니다.[1]'), \
                patch.object(rag, '_search') as search, \
                patch.object(db, 'execute'):
            result = rag.answer_question('요즘 누가 유명해?', 5)
        search.assert_not_called()
        self.assertEqual(result.domain, 'ent_culture')
        self.assertEqual(len(result.sources), 1)
        self.assertIn('A그룹', result.answer)

    def test_single_candidate_survives_generation_and_verification_insufficiency(self):
        items = [trend_service.TrendItem('A그룹', 1, 1, NOW, .9, (row(1, 'A그룹 컴백', 'A'),))]
        analysis = rule_query_analysis('요즘 어떤 아이돌이 유명해?')
        with patch.object(rag, '_query_analysis', return_value=(analysis, True)), \
                patch.object(trend_service, 'aggregate', return_value=(items, 1)), \
                patch.object(rag, 'generate_trend_answer', return_value=INSUFFICIENT_ANSWER), \
                patch.object(rag, 'verify_answer', return_value=INSUFFICIENT_ANSWER), \
                patch.object(rag, '_search') as search, patch.object(db, 'execute'):
            result = rag.answer_question(analysis.original_question, 5)
        search.assert_not_called()
        self.assertIn('A그룹', result.answer)
        self.assertIn('1건', result.answer)
        self.assertNotEqual(result.answer, INSUFFICIENT_ANSWER)
        self.assertIsNone(diagnostics.snapshot()['insufficient_reason'])

    def test_extraction_requires_real_name_and_quote(self):
        trend_service._extract_batch.cache_clear()
        passage = '그룹 스트레이 키즈가 신곡을 공개했다.'
        payload = [
            dict(name='스트레이 키즈', target_type='idol_or_group', article_index=0, evidence_quote=passage),
            dict(name='가상그룹', target_type='idol_or_group', article_index=0, evidence_quote='그룹 가상그룹'),
            dict(name='스트레이 키즈', target_type='actor', article_index=100, evidence_quote=passage),
        ]
        with patch.object(trend_service, 'extract_trend_entities', return_value=payload):
            self.assertEqual(trend_service._extract_batch((passage,)), (('스트레이 키즈', 'idol_or_group', 0),))
        trend_service._extract_batch.cache_clear()

    def test_type_filter_full_names_and_source_counts(self):
        rows = [row(1, '스트레이 키즈, 신곡 공개', 'A'), row(2, '스트레이 키즈의 공연', None),
                row(3, '김배우 드라마 출연', 'B')]
        with patch.object(trend_service, '_extract_batch', return_value=(
                ('스트레이 키즈', 'idol_or_group', 0), ('스트레이 키즈', 'idol_or_group', 1),
                ('김배우', 'actor', 2))):
            grounded = trend_service._entity_rows(rows, 'idol')
        result = trend_service.aggregate_rows(grounded, NOW, category_hint='idol')
        self.assertEqual([(item.name, item.mention_count, item.source_count) for item in result],
                         [('스트레이 키즈', 2, 2)])

    def test_trend_context_contains_representative_bodies(self):
        first = row(1, 'A그룹 새 앨범', 'A')
        first['content'] = 'A그룹이 새 앨범을 발매했다.'
        second = row(2, 'A그룹 공연', 'B')
        second['content'] = 'A그룹이 일본 공연을 진행했다.'
        items = [trend_service.TrendItem('A그룹', 2, 2, NOW, .9, (first, second))]
        analysis = rule_query_analysis('요즘 누가 유명해?')
        with patch.object(rag, '_query_analysis', return_value=(analysis, True)), \
                patch.object(trend_service, 'aggregate', return_value=(items, 2)), \
                patch.object(rag, 'generate_trend_answer', return_value='A그룹 새 앨범 발매와 일본 공연이 확인됩니다.[1][2]') as generate, \
                patch.object(rag, 'verify_answer', return_value='A그룹 새 앨범 발매와 일본 공연이 확인됩니다.[1][2]'), \
                patch.object(db, 'execute'):
            result = rag.answer_question(analysis.original_question, 5)
        self.assertIn(first['content'], generate.call_args.args[1])
        self.assertIn(second['content'], generate.call_args.args[1])
        self.assertEqual(len(result.sources), 2)

    def test_ambiguous_name_is_counted_only_in_recognized_articles(self):
        rows = [row(1, '가수 이제, 신곡 공개', 'A'), row(2, '다른그룹, 이제 컴백한다', 'B')]
        with patch.object(trend_service, '_extract_batch', return_value=(('이제', 'entertainer', 0),)):
            grounded = trend_service._entity_rows(rows, None)
        result = trend_service.aggregate_rows(grounded, NOW)
        self.assertEqual([(item.name, item.mention_count) for item in result], [('이제', 1)])

    def test_aggregator_articles_use_publisher_domains(self):
        rows = [row(1, 'A그룹 컴백', '네이버뉴스'), row(2, 'A그룹 공연', '네이버뉴스')]
        rows[0]['url'] = 'https://www.publisher-a.example/news/1'
        rows[1]['url'] = 'https://publisher-b.example/news/2'
        result = trend_service.aggregate_rows(rows, NOW)
        self.assertEqual(result[0].source_count, 2)

    def test_unknown_dates_and_sources_do_not_break_partial_ranking(self):
        rows = [row(1, 'A그룹 컴백', None), row(2, 'A그룹 공연', None)]
        for item in rows:
            item.update(published_at=None, url=None)
        result = trend_service.aggregate_rows(rows, NOW)
        self.assertEqual(result[0].source_count, 0)
        self.assertIsNone(result[0].latest_at)

    def test_db_role_is_shared_across_recognized_alias_mentions(self):
        rows = [row(1, 'BTS 공연', 'A'), row(2, '그룹 방탄소년단 소식', 'B')]
        with patch.object(trend_service, '_extract_batch', return_value=(
                ('BTS', 'entertainer', 0), ('방탄소년단', 'idol_or_group', 1))):
            grounded = trend_service._entity_rows(rows, 'idol')
        result = trend_service.aggregate_rows(grounded, NOW, category_hint='idol')
        self.assertEqual([(item.name, item.mention_count) for item in result], [('BTS', 2)])


if __name__ == '__main__':
    unittest.main()
