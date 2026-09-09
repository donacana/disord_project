import json
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from backend import db, main, openai_client, rag
from backend.answer_validator import INSUFFICIENT_ANSWER, remap_citations, sentences, validate_answer

EVIDENCE = '테스트그룹은 게임 컬래버를 진행했습니다. 테스트그룹 게임 컬래버 진행. 게임 브랜드와 협업.'
SUPPORTED = '테스트그룹은 게임 컬래버를 진행했습니다.[1]'


def article(identifier, content=EVIDENCE, summary=None):
    return dict(article_id=identifier, title=f'테스트 기사 {identifier}', content=content,
                summary=summary, source_name='테스트신문', published_at=datetime(2026, 9, 9),
                collected_at=datetime(2026, 9, 9, tzinfo=timezone.utc),
                url=f'https://example.com/{identifier}', similarity=.6)


class ValidatorTests(unittest.TestCase):
    def test_supported_sentence_and_citation_formats(self):
        self.assertEqual(validate_answer(SUPPORTED, [EVIDENCE]).citation_ids, (1,))
        for citation in ['[0]', '[7]', '[1,2]', '[1-2]', '[abc]']:
            result = validate_answer(SUPPORTED.replace('[1]', citation), [EVIDENCE])
            self.assertEqual(result.answer, INSUFFICIENT_ANSWER, citation)
        self.assertEqual(validate_answer(SUPPORTED.replace('[1]', ''), [EVIDENCE]).citation_ids, ())
        for dangling in [' [7', ' ]']:
            result = validate_answer(SUPPORTED + dangling, [EVIDENCE])
            self.assertEqual(result.answer, SUPPORTED)
            self.assertTrue(result.removed_sentences)

    def test_unsupported_specific_facts(self):
        unsupported = [
            '테스트그룹은 2026년 1월 데뷔했습니다.[1]',
            '테스트그룹은 두 번째 미니앨범을 발매했습니다.[1]',
            '테스트그룹은 글로벌 팬덤 100만 명을 보유했습니다.[1]',
            '테스트그룹은 오디션프로그램에서 데뷔했습니다.[1]',
            '테스트그룹은 "새로운앨범"을 발표했습니다.[1]',
            "테스트그룹은 '새로운앨범'을 발표했습니다.[1]",
            '테스트그룹은 ‘새로운앨범’을 발표했습니다.[1]',
            '테스트그룹은 새브랜드와 협업했습니다.[1]',
            '다른그룹은 게임 컬래버를 진행했습니다.[1]',
        ]
        for sentence in unsupported:
            result = validate_answer(SUPPORTED + '\n' + sentence, [EVIDENCE])
            self.assertEqual(result.answer, SUPPORTED, sentence)
            self.assertEqual(result.removed_sentences, (sentence,))

    def test_grounded_paraphrase_keeps_supported_sentence(self):
        result = validate_answer('테스트그룹은 게임 협업을 진행했습니다.[1]', [EVIDENCE])
        self.assertEqual(result.citation_ids, (1,))
        unsupported = validate_answer('테스트그룹은 큰 인기를 끌고 있습니다.[1]', [EVIDENCE])
        self.assertEqual(unsupported.answer, INSUFFICIENT_ANSWER)
        definition = validate_answer(
            '스트레이 키즈는 그룹이며 최근 새 앨범을 발매했습니다.[1]',
            ['그룹 스트레이 키즈가 새 앨범을 발매했습니다.'],
        )
        self.assertEqual(definition.citation_ids, (1,))

    def test_numeric_boundaries_and_units(self):
        evidence = '테스트그룹 2026년 11월 공연. 관객 12명. 가격 1.5만원.'
        for claim in ['테스트그룹 2026년 1월 공연.[1]', '관객 2명.[1]', '가격 1.6만원.[1]']:
            self.assertEqual(validate_answer(claim, [evidence]).citation_ids, ())
        self.assertEqual(validate_answer('가격 1.5만원.[1]', [evidence]).citation_ids, (1,))
        self.assertEqual(validate_answer('테스트그룹 2026년 11월 공연.[1]', [evidence]).citation_ids, (1,))

    def test_sentence_boundaries_and_citation_attachment(self):
        text = '테스트그룹 게임 컬래버 진행. [1] 미니앨범 발매.[7]\n- 게임 브랜드와 협업.[1]'
        self.assertEqual(len(sentences(text)), 3)
        result = validate_answer(text, [EVIDENCE])
        self.assertEqual(len(result.removed_sentences), 1)
        self.assertEqual(result.citation_ids, (1,))
        result = validate_answer('새앨범 발매. 테스트그룹 게임 컬래버 진행.[1]', [EVIDENCE])
        self.assertEqual(result.removed_sentences, ('새앨범 발매.',))

    def test_wrong_source_and_multi_source_claims(self):
        evidence = [EVIDENCE, '다른그룹 신곡 발표']
        self.assertEqual(validate_answer(SUPPORTED.replace('[1]', '[2]'), evidence).citation_ids, ())
        self.assertEqual(validate_answer(SUPPORTED.replace('[1]', '[1][2]'), evidence).citation_ids, ())
        self.assertEqual(validate_answer(SUPPORTED.replace('[1]', '[1][2]'), [EVIDENCE, EVIDENCE]).citation_ids, (1, 2))

    def test_extractive_mode_prevents_role_reversal_and_fragments(self):
        evidence = ['소속사는 가수를 보호했다. 가수는 소속사와 활동했다.']
        changed = '가수는 소속사를 보호했다.[1]'
        corrected = validate_answer(changed, evidence, require_extract=True)
        self.assertEqual(corrected.answer, '소속사는 가수를 보호했다.[1]')
        self.assertTrue(corrected.replaced_sentences)
        self.assertEqual(validate_answer('소속사는 가수를 보호했다.[1]', evidence, require_extract=True).citation_ids, (1,))
        self.assertEqual(validate_answer('가수를 보호했다.[1]', evidence, require_extract=True).answer,
                         '소속사는 가수를 보호했다.[1]')
        self.assertEqual(validate_answer(SUPPORTED, [EVIDENCE], require_extract=True).citation_ids, (1,))

    def test_cannot_combine_facts_from_different_source_sentences(self):
        evidence = ['테스트그룹 2026년 공연. 다른그룹 1월 데뷔.']
        result = validate_answer('테스트그룹 2026년 1월 데뷔.[1]', evidence, require_extract=True)
        self.assertEqual(result.answer, INSUFFICIENT_ANSWER)

    def test_contradictory_global_conclusion(self):
        result = validate_answer(SUPPORTED + '\n현재 수집된 자료에서 확인하기 어렵습니다.', [EVIDENCE])
        self.assertEqual(result.answer, SUPPORTED)
        self.assertEqual(result.citation_ids, (1,))
        self.assertEqual(result.removed_sentences, ('현재 수집된 자료에서 확인하기 어렵습니다.',))
        self.assertEqual(validate_answer('', [EVIDENCE]).answer, INSUFFICIENT_ANSWER)

    def test_only_visible_context_can_support_claims(self):
        row = article(1, content='테스트그룹 2026년 9월 데뷔.', summary=EVIDENCE)
        self.assertEqual(validate_answer('테스트그룹 2026년 9월 데뷔.[1]', rag._evidence([row])).citation_ids, ())
        row = article(1, content='가' * rag.MAX_CONTEXT_CHARS + '테스트그룹 미니앨범')
        self.assertEqual(validate_answer('테스트그룹 미니앨범.[1]', rag._evidence([row])).citation_ids, ())
        row = article(1)
        self.assertNotIn('2026', rag._evidence([row])[0])
        self.assertIn('ARTICLE 1 START', rag._context([row]))
        self.assertIn('ARTICLE 1 END', rag._context([row]))

    def test_remap_does_not_cascade(self):
        self.assertEqual(remap_citations('내용[2] 다른 내용[5] 내용[2]', (2, 5)),
                         '내용[1] 다른 내용[2] 내용[1]')


class PipelineTests(unittest.TestCase):
    def test_used_sources_and_log_are_compacted(self):
        rows = [article(1, '다른그룹 광고'), article(2)]
        supported_second = SUPPORTED.replace('[1]', '[2]')
        with patch.object(rag, '_search', return_value=rows), \
                patch.object(rag, 'generate_answer', return_value=supported_second), \
                patch.object(rag, 'verify_answer', return_value=supported_second) as verify, \
                patch.object(db, 'execute') as log:
            response = TestClient(main.app).post('/ask', json={'question': '테스트그룹 활동'})
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(set(data), {'answer', 'sources', 'domain'})
        self.assertEqual(data['answer'], SUPPORTED)
        self.assertEqual(len(data['sources']), 1)
        self.assertEqual(data['sources'][0]['url'], rows[1]['url'])
        self.assertEqual(set(data['sources'][0]), {'title', 'url', 'collected_at'})
        verify.assert_called_once()
        params = log.call_args.args[1]
        self.assertEqual(params[1], SUPPORTED)
        self.assertEqual(len(json.loads(params[2])), 1)
        self.assertTrue(params[4])

    def test_verifier_cannot_reintroduce_invented_facts(self):
        with patch.object(rag, '_search', return_value=[article(1)]), \
                patch.object(rag, 'generate_answer', return_value=SUPPORTED), \
                patch.object(rag, 'verify_answer', return_value='테스트그룹 2026년 데뷔.[1]'), \
                patch.object(db, 'execute') as log:
            result = rag.answer_question('테스트그룹 활동', 5)
        self.assertEqual(result.answer, INSUFFICIENT_ANSWER)
        self.assertEqual(result.sources, [])
        self.assertFalse(log.call_args.args[1][4])

    def test_verifier_failure_and_missing_results_fail_closed(self):
        with patch.object(rag, '_search', return_value=[article(1)]), \
                patch.object(rag, 'generate_answer', return_value=SUPPORTED), \
                patch.object(rag, 'verify_answer', side_effect=openai_client.OpenAIServiceError('failed')) as verify, \
                patch.object(db, 'execute'):
            result = rag.answer_question('테스트그룹 활동', 5)
        self.assertEqual(result.answer, INSUFFICIENT_ANSWER)
        self.assertEqual(result.sources, [])
        verify.assert_called_once()
        with patch.object(rag, '_search', return_value=[]), \
                patch.object(rag, 'generate_answer') as generate, \
                patch.object(rag, 'verify_answer') as verify, patch.object(db, 'execute'):
            result = rag.answer_question('활동', 5)
        generate.assert_not_called()
        verify.assert_not_called()
        self.assertEqual(result.answer, INSUFFICIENT_ANSWER)

    def test_semantic_verdict_overrides_lexical_overlap(self):
        # All words could be present while the subject/action relationship is
        # wrong. The verifier's conservative decision must survive the pipeline.
        with patch.object(rag, '_search', return_value=[article(1)]), \
                patch.object(rag, 'generate_answer', return_value=SUPPORTED), \
                patch.object(rag, 'verify_answer', return_value=INSUFFICIENT_ANSWER), \
                patch.object(db, 'execute'):
            result = rag.answer_question('컴백 공연이 활발한 그룹', 5)
        self.assertEqual(result.sources, [])
        self.assertEqual(result.answer, INSUFFICIENT_ANSWER)

    def test_same_chat_model_and_one_verification_request(self):
        client = Mock()
        client.chat.completions.create.return_value = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=SUPPORTED))])
        with patch.dict('os.environ', {'OPENAI_CHAT_MODEL': 'configured-model'}), \
                patch.object(openai_client, '_client', return_value=client):
            openai_client.generate_answer('활동', EVIDENCE)
            openai_client.verify_answer('활동', EVIDENCE, SUPPORTED)
        self.assertEqual(client.chat.completions.create.call_count, 2)
        self.assertEqual([call.kwargs['model'] for call in client.chat.completions.create.call_args_list],
                         ['configured-model', 'configured-model'])

    def test_answer_prompts_allow_grounded_detail(self):
        client = Mock()
        client.chat.completions.create.return_value = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=SUPPORTED))])
        with patch.object(openai_client, '_client', return_value=client):
            openai_client.generate_answer('테스트그룹 최근 활동', EVIDENCE)
            openai_client.verify_answer('테스트그룹 최근 활동', EVIDENCE, SUPPORTED)
        generation_prompt = client.chat.completions.create.call_args_list[0].kwargs['messages'][0]['content']
        verification_prompt = client.chat.completions.create.call_args_list[1].kwargs['messages'][0]['content']
        self.assertIn('최대 6개의 짧은 문장', generation_prompt)
        self.assertIn('기사 제목을 그대로 반복하지 말고', generation_prompt)
        self.assertIn('최대 6개까지 유지하라', verification_prompt)

    def test_multiple_grounded_sentences_survive(self):
        evidence = [
            '테스트그룹은 새 앨범을 발매했습니다. 테스트그룹은 공연을 진행했습니다.',
            '테스트그룹은 방송에 출연했습니다.',
        ]
        answer = ('테스트그룹은 새 앨범을 발매했습니다.[1]\n'
                  '테스트그룹은 공연을 진행했습니다.[1]\n'
                  '테스트그룹은 방송에 출연했습니다.[2]')
        result = validate_answer(answer, evidence)
        self.assertEqual(result.citation_ids, (1, 2))
        self.assertEqual(len(result.answer.splitlines()), 3)
        unsupported = validate_answer(answer + '\n테스트그룹은 세계 최고입니다.[1]', evidence)
        self.assertEqual(len(unsupported.answer.splitlines()), 3)
        self.assertTrue(unsupported.removed_sentences)

    def test_generation_failure_keeps_503_contract(self):
        with patch.object(rag, '_search', return_value=[article(1)]), \
                patch.object(rag, 'generate_answer', side_effect=openai_client.OpenAIServiceError('failed')), \
                patch.object(rag, 'verify_answer') as verify:
            response = TestClient(main.app).post('/ask', json={'question': '활동'})
        self.assertEqual(response.status_code, 503)
        verify.assert_not_called()

    def test_title_fallback_preserves_only_explicit_question_evidence(self):
        row = article(1, '영화 관련 인터뷰')
        row['title'] = "영화 '테스트작품', 11월 개봉"
        reviewed = "영화 '테스트작품'은 11월 20일 개봉합니다.[1]"
        checked = validate_answer(reviewed, rag._evidence([row]), require_extract=True)
        self.assertEqual(checked.citation_ids, ())
        result = rag._title_fallback('최근 영화 개봉작 알려줘', reviewed, [row], checked)
        self.assertEqual(result.answer, row['title'] + '[1]')
        self.assertNotIn('20일', result.answer)
        insufficient = validate_answer(INSUFFICIENT_ANSWER, rag._evidence([row]), require_extract=True)
        self.assertEqual(rag._title_fallback('최근 영화 개봉작 알려줘', INSUFFICIENT_ANSWER,
                                            [row], insufficient).answer, INSUFFICIENT_ANSWER)
        row['title'] = '테스트그룹 게임 컬래버'
        reviewed = '테스트그룹은 공연이 활발합니다.[1]'
        checked = validate_answer(reviewed, rag._evidence([row]), require_extract=True)
        self.assertEqual(rag._title_fallback('테스트그룹 공연', reviewed, [row], checked).answer,
                         INSUFFICIENT_ANSWER)


if __name__ == '__main__':
    unittest.main()
