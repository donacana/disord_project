import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from fastapi.testclient import TestClient

from backend import config, db, main, openai_client, rag, retrieval
from backend.query_utils import (analyze_query, expand_query, extract_definition_entity,
                                 extract_keywords, merge_llm_analysis, rule_query_analysis,
                                 should_use_llm)

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
        self.assertEqual([r['article_id'] for r in ranked], [3, 2])
        self.assertNotIn('final_score', rows[0])

    def test_threshold_candidates_and_no_padding(self):
        rows = [article(1, '아이브', .29), article(2, '무관 기사', .6)]
        with patch.object(db, 'fetch_all', return_value=rows) as fetch:
            result = retrieval.search([0.1], '아이브', 5, .3)
        self.assertEqual(fetch.call_args.args[1][-1], 40)
        self.assertEqual([r['article_id'] for r in result], [1])
        self.assertEqual(retrieval.rank_candidates(rows, '아이브', 5, .9), [])

    def test_recency_and_no_keyword_fallback(self):
        for days, expected in [(0, 1), (7, 1), (8, .7), (30, .7), (31, .3), (90, .3), (91, 0)]:
            self.assertEqual(retrieval.recency_score(NOW - timedelta(days=days), NOW), expected)
        self.assertEqual(retrieval.recency_score(None, NOW), 0)
        self.assertEqual(retrieval.recency_score('invalid', NOW), 0)
        self.assertEqual(retrieval.recency_score(NOW.replace(tzinfo=None), NOW), 1)
        rows = [article(1, '영화 개봉 소식', days=100), article(2, '영화 개봉 일정')]
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

    def test_query_intents_and_entities(self):
        cases = [
            ('아이브 요즘 활동 뭐 했어?', ('아이브',), 'activity', (), 30),
            ('장원영 최근 광고 뭐 찍었어?', ('장원영',), 'advertisement', ('celeb',), 30),
            ('최근 영화 개봉작 알려줘', (), 'movie_release', ('movie',), 30),
            ('아이브 논란 있었어?', ('아이브',), 'controversy', (), None),
            ('뉴진스 공연 일정 알려줘', ('뉴진스',), 'event', ('event', 'music'), None),
            ('최근 연예계 주요 이슈 알려줘', (), None, (), 30),
            ('이번 주 영화 소식', (), 'movie', ('movie',), 7),
            ('이번달 배우 최근 활동', (), 'activity', ('celeb', 'drama'), 30),
            ('아이돌 컴백 알려줘', (), 'music_release', ('music',), None),
        ]
        for question, entities, intent, categories, days in cases:
            hints = analyze_query(question)
            self.assertEqual((hints.entities, hints.intent, hints.categories, hints.recent_days),
                             (entities, intent, categories, days))

    def test_definition_activity_and_query_expansion(self):
        definition_questions = [
            '스트레이 키즈가 누군데',
            '스트레이 키즈는 누구야',
            '스트레이 키즈 어떤 그룹이야',
            '알파드라이브원은 누구야',
        ]
        for question in definition_questions:
            hints = analyze_query(question)
            self.assertEqual(hints.intent, 'definition', question)
            self.assertEqual(len(hints.entities), 1, question)
            self.assertIn(hints.entities[0], question, question)
            definition = retrieval.rank_candidates(
                [article(1, f'{hints.entities[0]}, 새 앨범으로 컴백', .28)],
                question, 5, now=NOW)
            self.assertEqual([row['article_id'] for row in definition], [1], question)
        self.assertEqual(extract_definition_entity('스트레이 키즈가 누군데'), '스트레이 키즈')
        self.assertEqual(extract_definition_entity('알파드라이브원은 누구야'), '알파드라이브원')
        self.assertIn('스트레이 키즈 데뷔', expand_query('스트레이 키즈는 누구야'))

        row = article(1, '스트레이 키즈 새 앨범 발표', .21)
        row['category'] = 'trend'
        self.assertEqual(retrieval.rank_candidates([row], '스트레이 키즈는 누구야', 5, now=NOW)[0]['article_id'], 1)

        activity = retrieval.rank_candidates(
            [article(1, '장원영 광고 화보 공개', .45)],
            '장원영 최근 활동 알려줘', 5, now=NOW)
        self.assertEqual([row['article_id'] for row in activity], [1])

        controversy = retrieval.rank_candidates(
            [article(1, '장원영 광고 화보', .9), article(2, '장원영 법적 대응 논란', .45)],
            '장원영 논란 있었어?', 5, now=NOW)
        self.assertEqual(controversy[0]['article_id'], 2)

    def test_structured_query_analysis_and_llm_fallback_policy(self):
        cases = [
            ('스트레이 키즈가 누군데', '스트레이 키즈', 'definition', 'unknown'),
            ('스키즈 요즘 뭐함?', '스트레이 키즈', 'activity', 'recent'),
            ('장원영 무슨 일 있었음?', '장원영', 'controversy', 'unknown'),
            ('최근 컴백한 아이돌 그룹 알려줘', None, 'comeback', 'recent'),
            ('요즘 누가 유명해?', None, 'trend_ranking', 'recent'),
        ]
        for question, entity, intent, time_range in cases:
            analysis = rule_query_analysis(question)
            self.assertEqual((analysis.entity, analysis.intent, analysis.time_range),
                             (entity, intent, time_range), question)
        self.assertTrue(should_use_llm(rule_query_analysis('스키즈 요즘 뭐함?')))
        self.assertFalse(should_use_llm(rule_query_analysis('장원영 최근 활동 알려줘')))
        rule = rule_query_analysis('장원영 최근 활동 알려줘')
        invalid = merge_llm_analysis(rule, {'intent': 'made_up_intent'})
        self.assertEqual(invalid.intent, 'general')

    def test_llm_query_analysis_json_and_failure(self):
        response = type('Response', (), {'choices': [type('Choice', (), {
            'message': type('Message', (), {'content': '{"original_question":"스키즈 요즘 뭐함?","entity":"스트레이 키즈","intent":"activity","time_range":"recent","keywords":["앨범"],"search_queries":["스트레이 키즈 최근 활동","스트레이 키즈 앨범"],"normalized_question":"스트레이 키즈 최근 활동","confidence":0.92}'})()
        })()]})()
        client = type('Client', (), {'chat': type('Chat', (), {
            'completions': type('Completions', (), {'create': lambda self, **kwargs: response})()
        })()})()
        with patch.object(openai_client, '_client', return_value=client):
            payload = openai_client.analyze_question('스키즈 요즘 뭐함?')
        self.assertEqual(payload['entity'], '스트레이 키즈')
        self.assertEqual(payload['intent'], 'activity')
        with patch.object(openai_client, '_client', side_effect=openai_client.OpenAIServiceError('timeout')):
            with self.assertRaises(openai_client.OpenAIServiceError):
                openai_client.analyze_question('스키즈 요즘 뭐함?')

    def test_query_understanding_is_rule_first_and_failure_safe(self):
        with patch.object(openai_client, 'analyze_question', return_value={
            'original_question': '장원영 최근 활동 알려줘',
            'entity': '장원영', 'intent': 'activity', 'time_range': 'recent',
            'keywords': ['활동'], 'search_queries': ['장원영 최근 활동'],
            'normalized_question': '장원영 최근 활동', 'confidence': 0.9,
        }) as analyze:
            result, used = rag._query_analysis('장원영 최근 활동 알려줘')
        analyze.assert_called_once()
        self.assertTrue(used)
        self.assertEqual((result.entity, result.intent), ('장원영', 'activity'))

        with patch.object(openai_client, 'analyze_question', return_value={
            'entity': '스트레이 키즈', 'intent': 'activity', 'time_range': 'recent',
            'keywords': ['컴백', '앨범'], 'normalized_question': '스트레이 키즈 최근 활동',
            'confidence': 0.92,
        }) as analyze:
            result, used = rag._query_analysis('스키즈 요즘 뭐함?')
        analyze.assert_called_once()
        self.assertTrue(used)
        self.assertEqual((result.entity, result.intent, result.time_range),
                         ('스트레이 키즈', 'activity', 'recent'))

        with patch.object(openai_client, 'analyze_question',
                          side_effect=openai_client.OpenAIServiceError('timeout')):
            result, used = rag._query_analysis('스키즈 요즘 뭐함?')
        self.assertFalse(used)
        self.assertEqual((result.entity, result.intent), ('스트레이 키즈', 'activity'))

    def test_low_confidence_uses_rule_fallback(self):
        with patch.object(openai_client, 'analyze_question', return_value={
            'original_question': '장원영 최근 활동 알려줘',
            'entity': '장원영', 'intent': 'activity', 'time_range': 'recent',
            'keywords': [], 'search_queries': ['장원영'],
            'normalized_question': '장원영', 'confidence': 0.2,
        }):
            result, used = rag._query_analysis('장원영 최근 활동 알려줘')
        self.assertFalse(used)
        self.assertEqual((result.entity, result.intent), ('장원영', 'activity'))

    def test_activity_vs_controversy(self):
        rows = [article(1, '테스트가수 고소 사건 법원 판결', .8, '공연 이력'),
                article(2, '테스트가수 앨범 발매 공연', .5)]
        activity = retrieval.rank_candidates(rows, '테스트가수 최근 활동 알려줘', 5, now=NOW)
        self.assertEqual([r['article_id'] for r in activity], [2, 1])
        controversy = retrieval.rank_candidates(rows, '테스트가수 논란 있었어?', 5, now=NOW)
        self.assertEqual(controversy[0]['article_id'], 1)

    def test_recent_first_old_and_null_fallback(self):
        rows = [article(1, '테스트가수 공연 과거', .85, days=100),
                article(2, '테스트가수 공연 최신', .5),
                article(3, '테스트가수 공연 미상', .4)]
        rows[2]['published_at'] = None
        rows[2]['collected_at'] = NOW
        ranked = retrieval.rank_candidates(rows, '테스트가수 이번주 공연', 5, now=NOW)
        self.assertEqual([r['article_id'] for r in ranked], [2, 1, 3])
        self.assertEqual(ranked[2]['recency_score'], 0)
        rows[0]['published_at'] = NOW - timedelta(days=8)
        self.assertEqual(retrieval.rank_candidates(rows, '테스트가수 이번주 공연', 1, now=NOW)[0]['article_id'], 2)
        self.assertEqual(retrieval.rank_candidates(rows, '테스트가수 이번달 공연', 1, now=NOW)[0]['article_id'], 1)

    def test_category_penalty_is_not_hard_filter(self):
        rows = [article(1, '영화 개봉 일정', .6), article(2, '영화 개봉 인터뷰', .6)]
        rows[1]['category'] = 'celeb'
        ranked = retrieval.rank_candidates(rows, '최근 영화 개봉작', 5, now=NOW)
        self.assertEqual([r['article_id'] for r in ranked], [1, 2])
        self.assertGreater(ranked[0]['final_score'], ranked[1]['final_score'])

    def test_movie_release_does_not_mean_any_theater_news(self):
        rows = [article(1, '극장 위기 영화산업 정책', .8),
                article(2, '영화 자막 제공 확정판결', .8, '작품 개봉 후 소송'),
                article(3, '영화 개봉주 무대인사', .45)]
        rows[2]['category'] = 'webtoon'
        ranked = retrieval.rank_candidates(rows, '최근 영화 개봉작 알려줘', 5, now=NOW)
        self.assertEqual([r['article_id'] for r in ranked], [1, 3, 2])

    def test_source_diversity_and_shortage(self):
        rows = [article(i, f'문화 소식 {i}', .7 - i * .005) for i in range(1, 7)]
        for row, source in zip(rows, ['A', 'A', 'A', 'B', 'B', 'C']):
            row['source_name'] = source
        ranked = retrieval.rank_candidates(rows, '최근 연예계 주요 이슈 알려줘', 5, now=NOW)
        self.assertEqual([r['source_name'] for r in ranked], ['A', 'A', 'B', 'B', 'C'])
        for row in rows:
            row['source_name'] = 'A'
        self.assertEqual(len(retrieval.rank_candidates(rows, '최근 연예계 주요 이슈 알려줘', 5, now=NOW)), 5)
        rows[-1]['source_name'] = 'B'
        rows[-1]['distance'] = .65
        self.assertNotIn(6, [r['article_id'] for r in retrieval.rank_candidates(rows, '최근 소식', 5, now=NOW)])

    def test_deduplication_and_collection_tiebreak(self):
        rows = [article(1, '문화 소식 A'), article(1, '문화 소식 B'),
                article(3, '문화 소식 C'), article(4, '문화 소식 D'),
                article(5, '문화  소식 D!'), article(6, '같은 사건 다른 관점')]
        rows[2]['url'] = rows[0]['url']
        ranked = retrieval.rank_candidates(rows, '최근 소식', 10, now=NOW)
        self.assertEqual([r['article_id'] for r in ranked], [1, 4, 6])
        rows = [article(1, '동일 제목'), article(2, '동일 제목')]
        rows[1]['collected_at'] = NOW + timedelta(hours=1)
        self.assertEqual(retrieval.rank_candidates(rows, '최근 소식', 5, now=NOW)[0]['article_id'], 2)

    def test_indirect_result_limit_and_relevance_before_diversity(self):
        rows = [article(i, f'테스트가수 광고 {i}', .5) for i in range(1, 4)]
        rows += [article(i, f'브랜드 광고 {i}', .95) for i in range(4, 8)]
        for row in rows:
            row['category'] = 'celeb'
            row['source_name'] = 'A' if row['article_id'] < 4 else str(row['article_id'])
        ranked = retrieval.rank_candidates(rows, '테스트가수 광고', 5, now=NOW)
        self.assertEqual([r['article_id'] for r in ranked[:3]], [1, 2, 3])
        self.assertEqual(len(ranked), 4)
        only_indirect = retrieval.rank_candidates(rows[3:], '테스트가수 광고', 5, now=NOW)
        self.assertEqual(len(only_indirect), 1)

    def test_invalid_scores_empty_metadata_and_rescue_bounds(self):
        rows = [article(1, '테스트가수'), article(2, '테스트가수 낮은 유사도', .1)]
        rows[0]['distance'] = float('nan')
        self.assertEqual(retrieval.rank_candidates(rows, '테스트가수', 5, now=NOW), [])
        for distance in [None, 'invalid', float('inf')]:
            rows[0]['distance'] = distance
            self.assertEqual(retrieval.rank_candidates(rows, '테스트가수', 5, now=NOW), [])
        rows = [article(1, '', .7), article(2, '', .7)]
        for row in rows:
            row.update(source_name=None, published_at=None, collected_at=None, url=None)
        self.assertEqual(len(retrieval.rank_candidates(rows, '최근 소식', 5, now=NOW)), 2)
        rescued = retrieval.rank_candidates([article(1, '테스트가수', .29)], '테스트가수', 5, .3, NOW)
        self.assertEqual(len(rescued), 1)
        self.assertEqual(retrieval.rank_candidates([article(1, '테스트가수', .29)], '테스트가수', 5, .5, NOW), [])
        weak_title = retrieval.rank_candidates(
            [article(1, '테스트가수 새 앨범 발표', .16)], '테스트가수 최근 활동', 5, .3, NOW)
        self.assertEqual([row['article_id'] for row in weak_title], [1])
        weak_body = retrieval.rank_candidates(
            [article(1, '새 앨범 발표', .16, content='테스트가수의 새 앨범 활동')],
            '테스트가수 최근 활동', 5, .3, NOW)
        self.assertEqual([row['article_id'] for row in weak_body], [1])
        weights = [config.VECTOR_WEIGHT, config.KEYWORD_WEIGHT, config.TITLE_WEIGHT, config.RECENCY_WEIGHT,
                   config.ENTITY_WEIGHT, config.INTENT_WEIGHT, config.CATEGORY_WEIGHT]
        self.assertAlmostEqual(sum(weights), 1.0)

    @patch.object(rag, '_query_analysis', side_effect=lambda q: (rule_query_analysis(q), False))
    def test_api_contract_context_and_logging(self, _analysis):
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
                patch.object(rag, 'verify_answer', return_value='아이브 월드투어 [1]'), \
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
