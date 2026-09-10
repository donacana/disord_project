import unittest
import tempfile
from pathlib import Path
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
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        cache = patch.object(trend_service.config, 'TREND_ENTITY_CACHE_PATH', str(Path(directory.name) / 'entities.sqlite3'))
        cache.start()
        self.addCleanup(cache.stop)

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

    def test_popularity_reason_uses_normal_rag_without_trend_aggregation(self):
        analysis = rule_query_analysis('왜 요즘 디원이 유명해졌어?')
        article = row(1, '디원 군악대 합격과 입대 예정', 'A')
        article['content'] = '디원이 군악대 최종 합격 소식과 입대 예정 소식으로 최근 보도됐다.'
        article['summary'] = article['content']
        article['similarity'] = 0.9
        with patch.object(rag, '_query_analysis', return_value=(analysis, False)), \
                patch.object(rag.trend_service, 'aggregate') as aggregate, \
                patch.object(rag, '_search', return_value=[article]), \
                patch.object(rag, 'generate_answer', return_value='최근 군악대 합격 소식이 보도됐습니다.[1]'), \
                patch.object(rag, 'verify_answer', return_value='최근 군악대 합격 소식이 보도됐습니다.[1]'), \
                patch.object(db, 'execute'):
            result = rag.answer_question(analysis.original_question, 5)
        aggregate.assert_not_called()
        self.assertIn('군악대', result.answer)

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
        passage = '그룹 스트레이 키즈가 신곡을 공개했다.'
        payload = [
            dict(name='스트레이 키즈', target_type='idol_or_group', article_index=0, evidence_quote=passage),
            dict(name='가상그룹', target_type='idol_or_group', article_index=0, evidence_quote='그룹 가상그룹'),
            dict(name='스트레이 키즈', target_type='actor', article_index=100, evidence_quote=passage),
        ]
        with patch.object(trend_service, 'extract_trend_entities', return_value=payload):
            self.assertEqual(trend_service._extract_batch((passage,)), (('스트레이 키즈', 'idol_or_group', 0),))

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

    def test_work_extractors_do_not_rank_people(self):
        song_title = "타이틀곡 'BORN DIRE' 스윙스 참여"
        movie_title = "영화 '타짜: 벨제붑의 노래' 박스오피스 진입"
        drama_title = "디즈니+ 오리지널 시리즈 '메이드 인 코리아 시즌2' 방영"
        self.assertEqual(trend_service.extract_song_candidates(song_title), ['BORN DIRE'])
        self.assertEqual(trend_service.extract_movie_candidates(movie_title), ['타짜: 벨제붑의 노래'])
        self.assertEqual(trend_service.extract_drama_candidates(drama_title), ['메이드 인 코리아 시즌2'])

        rows = [row(1, song_title, 'A'), row(2, song_title, 'B'),
                row(3, '스윙스 김유정 현빈 르세라핌 근황', 'C')]
        result = trend_service.aggregate_rows(rows, NOW, top_k=5, category_hint='song')
        self.assertEqual([item.name for item in result], ['BORN DIRE'])
        self.assertEqual(result[0].candidate_type, 'song')

    def test_target_type_extractor_diagnostics_and_empty_type_result(self):
        with patch.object(db, 'fetch_all', return_value=[
            row(1, '스윙스 김유정 현빈 르세라핌 근황', 'A')
        ]), patch.object(trend_service, '_work_entity_rows', wraps=trend_service._work_entity_rows) as extract:
            results, _ = trend_service.aggregate('recent', category_hint='song', now=NOW)
        self.assertEqual(results, [])
        self.assertEqual(extract.call_args.args[1], 'song')
        self.assertEqual(diagnostics.snapshot()['extractor_used'], 'extract_song_candidates')

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
