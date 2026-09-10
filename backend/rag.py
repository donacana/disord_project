import json
import logging
import os
import time
from datetime import datetime

if __package__:
    from . import config, diagnostics
    from . import db
    from .openai_client import (OpenAIServiceError, embed_question, generate_answer,
                                generate_trend_answer, verify_answer)
    from .answer_validator import (CITATION, INSUFFICIENT_ANSWER, remap_citations,
                                   sentences, validate_answer)
    from .query_utils import (ARTICLE_INTENT_TERMS, GENERAL_TERMS, QueryAnalysis, analyze_query,
                               expand_query, merge_llm_analysis, rule_query_analysis)
    from .retrieval import MIN_SIMILARITY, search
    from . import trend_service
    from .schemas import AskResponse, SourceItem
else:
    import config
    import diagnostics
    import db
    from openai_client import (OpenAIServiceError, embed_question, generate_answer,
                               generate_trend_answer, verify_answer)
    from answer_validator import (CITATION, INSUFFICIENT_ANSWER, remap_citations,
                                 sentences, validate_answer)
    from query_utils import (ARTICLE_INTENT_TERMS, GENERAL_TERMS, QueryAnalysis, analyze_query,
                             expand_query, merge_llm_analysis, rule_query_analysis)
    from retrieval import MIN_SIMILARITY, search
    import trend_service
    from schemas import AskResponse, SourceItem


MAX_CONTEXT_CHARS = 4000
logger = logging.getLogger(__name__)


class RAGError(RuntimeError):
    """Raised when embedding or answer generation fails."""


def _query_analysis(question: str) -> tuple[QueryAnalysis, bool]:
    rule = rule_query_analysis(question)
    try:
        from .openai_client import analyze_question as llm_analyze_question
    except ImportError:
        from openai_client import analyze_question as llm_analyze_question
    try:
        analysis = merge_llm_analysis(rule, llm_analyze_question(question))
        if analysis.confidence < config.QUERY_UNDERSTANDING_MIN_CONFIDENCE:
            logger.warning('Query understanding confidence is low; using rule-based analysis.')
            return rule, False
        return analysis, True
    except OpenAIServiceError:
        logger.warning('Query understanding failed; using rule-based analysis.')
        return rule, False


def _search(question: str, top_k: int, analysis: QueryAnalysis | None = None) -> list[dict]:
    try:
        analysis = analysis or _query_analysis(question)[0]
        queries = analysis.search_queries or expand_query(question, analysis)
        embedding_query = ' '.join(dict.fromkeys(
            [analysis.entity or '', analysis.normalized_question, *analysis.keywords, *queries]
        ))
        embedding = embed_question(embedding_query[:1000])
        return search(embedding, question, top_k, MIN_SIMILARITY, analysis=analysis)
    except (db.DatabaseError, OpenAIServiceError) as error:
        raise RAGError(str(error)) from error

def _article_body(article: dict) -> str:
    return (article['summary'] or article['content'] or '')[:MAX_CONTEXT_CHARS]


def _evidence(results: list[dict]) -> list[str]:
    # No unseen full content, URLs or publication dates can substantiate a
    # factual claim. Validate against the exact title/body sent to the model.
    return [f'{article["title"]}\n{_article_body(article)}' for article in results]


def _has_related_evidence(question: str, results: list[dict], analysis: QueryAnalysis) -> bool:
    if not results:
        return False
    texts = [text.casefold() for text in _evidence(results)]
    if analysis.entity and any(analysis.entity.casefold() in text for text in texts):
        return True
    hints = analyze_query(question)
    keywords = [word.casefold() for word in hints.keywords
                if word.casefold() not in {term.casefold() for term in GENERAL_TERMS}
                and word.casefold() not in {'최근', '요즘', '알려줘'}]
    return bool(keywords) and any(any(word in text for word in keywords) for text in texts)


def _context(results: list[dict]) -> str:
    sections = []
    for index, article in enumerate(results, start=1):
        published_at = article['published_at'] or '미상'
        body = _article_body(article)
        sections.append(
            f'--- ARTICLE {index} START ---\n'
            f'[{index}]\n'
            f'제목: {article["title"]}\n'
            f'출처: {article["source_name"]}\n'
            f'발행일: {published_at}\n'
            f'본문: {body}\n'
            f'URL: {article["url"]}\n'
            f'--- ARTICLE {index} END ---'
        )
    return '\n\n'.join(sections)


def _source(article: dict) -> SourceItem:
    return SourceItem(
        title=article['title'],
        url=article['url'],
        collected_at=article['collected_at'],
    )


def _title_fallback(question: str, reviewed: str, results: list[dict], checked,
                    analysis: QueryAnalysis | None = None):
    """Keep a directly relevant headline when an over-detailed review is pruned.

    Preserve explicit article facts even if the generated draft was rejected;
    never infer performances/releases from a generic collaboration headline.
    """
    hints = analyze_query(question)
    if analysis and analysis.entity:
        hints = type(hints)(hints.keywords, (analysis.entity,), hints.intent, hints.categories,
                            hints.strict_category, hints.recent_days)
    if checked.citation_ids:
        return checked
    terms = ARTICLE_INTENT_TERMS.get(hints.intent, ())
    if not terms and hints.intent != 'definition':
        return checked
    lines = []
    cited_numbers = sorted(set(map(int, CITATION.findall(reviewed))))
    numbers = cited_numbers or list(range(1, len(results) + 1))
    for number in numbers:
        if not 1 <= number <= len(results):
            continue
        title = results[number - 1]['title']
        folded = title.casefold()
        direct_entity = bool(hints.entities) and all(entity.casefold() in folded for entity in hints.entities)
        title_has_intent = any(term in folded for term in terms)
        if not ((hints.intent == 'definition' and direct_entity)
                or (title_has_intent and (direct_entity or not hints.entities))):
            continue
        # A specific release/movie/drama intent can be sufficient without an
        # entity. Otherwise require a question keyword in the title as well.
        if not hints.entities and not hints.strict_category and not any(word in folded for word in hints.keywords):
            continue
        lines.append(f'{title}[{number}]')
    fallback = validate_answer('\n'.join(lines), _evidence(results), require_extract=True)
    return fallback if fallback.citation_ids else checked


def _log_result(question: str, answer: str, results: list[dict], top_score: float | None,
                is_answered: bool, latency_ms: int) -> None:
    sources = [
        {
            'title': article['title'],
            'url': article['url'],
            'collected_at': article['collected_at'].isoformat() if isinstance(article['collected_at'], datetime) else None,
            'score': article.get('similarity'),
        }
        for article in results
    ]
    try:
        db.execute(
            'INSERT INTO public.qa_logs '
            '(question, answer, sources_json, top_score, is_answered, latency_ms) '
            'VALUES (%s, %s, %s::jsonb, %s, %s, %s)',
            (question, answer, json.dumps(sources, ensure_ascii=False), top_score, is_answered, latency_ms),
        )
    except db.DatabaseError:
        return


def _trend_category_hint(analysis: QueryAnalysis) -> str | None:
    if analysis.target_type == 'idol_or_group':
        return 'idol'
    if analysis.target_type == 'actor':
        return 'actor'
    question = analysis.original_question.casefold()
    if any(term in question for term in ('아이돌', '걸그룹', '보이그룹', '그룹')):
        return 'idol'
    if any(term in question for term in ('배우', '연기자')):
        return 'actor'
    return None


def _answer_trend(question: str, analysis: QueryAnalysis, top_k: int) -> AskResponse:
    started = time.perf_counter()
    now = datetime.now().astimezone()
    items, scanned = trend_service.aggregate(
        analysis.time_range, top_k=min(top_k, config.TREND_TOP_K),
        category_hint=_trend_category_hint(analysis), now=now,
    )
    diagnostics.record(articles_scanned=scanned, top_entities=[
        {'name': item.name, 'mention_count': item.mention_count,
         'source_count': item.source_count, 'score': round(item.score, 4)} for item in items])
    if not items:
        answer = INSUFFICIENT_ANSWER
        trace = diagnostics.snapshot()
        reason = ('no_articles' if not scanned else 'no_entity_candidates'
                  if not trace.get('raw_entities') else 'no_eligible_entities_after_fallback')
        diagnostics.finish(generated_answer='', validated_answer='', final_answer=answer,
                           insufficient_reason=reason)
        _log_result(question, answer, [], None, False, int((time.perf_counter() - started) * 1000))
        return AskResponse(answer=answer, sources=[], domain='ent_culture')

    start = trend_service.period_start(analysis.time_range, now)
    scope = (f'현재 순위는 {start.date().isoformat()}부터 {now.date().isoformat()}까지 발행된 '
             f'현재 DB 수집 기사 중 최신 {scanned}건을 조회한 결과이며, '
             '기사 언급량, 출처 다양성, 최근성을 반영한 순위입니다. '
             '전체 대중 인기도나 긍정적인 활동량 순위는 아닙니다.')
    if diagnostics.snapshot().get('entity_extraction_failures'):
        scope += ' 일부 기사 묶음의 인물 분석이 완료되지 않아 확인된 후보만 포함한 부분 집계입니다.'
    results, evidence, fallback, manifest = [], [], [], []
    for item in items:
        representative = item.articles[0] if item.articles else {}
        index = len(results) + 1
        manifest.append({'name': item.name, 'mention_count': item.mention_count,
                         'source_count': item.source_count, 'evidence_ids': [index]})
        metric = (f'현재 수집된 기사 기준 상위 집계 대상은 {item.name}입니다. '
                  f'{item.name}의 기사 언급량은 {item.mention_count}건, 서로 다른 출처 수는 URL 도메인 기준 {item.source_count}곳입니다.')
        article = dict(representative)
        article.setdefault('title', item.name)
        article.setdefault('url', '')
        article.setdefault('collected_at', None)
        article['similarity'] = item.score
        results.append(article)
        body = (representative.get('summary') or representative.get('content') or '')[:MAX_CONTEXT_CHARS]
        evidence.append(metric + '\n' + scope + '\n' + article['title'] + '\n' + body)
        # This path is a deterministic extract of real aggregate facts and titles.
        fallback.extend(unit + f'[{index}]' for unit in sentences(metric))
        if representative.get('title'):
            fallback.append(representative['title'] + f'[{index}]')
        for other in item.articles[1:]:
            if other.get('url') == representative.get('url'):
                continue
            results.append(dict(other, similarity=item.score))
            manifest[-1]['evidence_ids'].append(len(results))
            evidence.append((other.get('title') or '') + '\n' +
                            (other.get('summary') or other.get('content') or '')[:MAX_CONTEXT_CHARS])
    fallback.extend(unit + '[1]' for unit in sentences(scope))
    intro = '현재 수집된 기사 기준으로는 ' + ', '.join(item.name for item in items) + ' 등이 상위 집계 대상입니다.'
    evidence[0] = intro + '\n' + evidence[0]
    context = ('RANKED_ENTITIES: ' + json.dumps(manifest, ensure_ascii=False) + '\n\n' +
               '\n\n'.join(f'[{i}]\n{text}' for i, text in enumerate(evidence, 1)))
    try:
        draft = generate_trend_answer(question, context)
    except OpenAIServiceError:
        draft = '\n'.join(fallback)
    try:
        reviewed = verify_answer(question, context, draft)
        checked = validate_answer(reviewed, evidence, semantic_verified=True)
    except OpenAIServiceError:
        checked = validate_answer(draft, evidence, require_extract=True)
    if not checked.citation_ids:
        checked = validate_answer('\n'.join(fallback), evidence)
    # Always retain the direct DB-backed conclusion and sampling scope. The
    # semantic reviewer must not turn a ranked answer into orphaned details.
    scope_units = sentences(scope)
    details = [unit for unit in sentences(checked.answer) if checked.citation_ids
               and CITATION.sub('', unit).strip() not in {*scope_units, intro}
               and '상위 집계 대상' not in unit and not unit.startswith('현재 순위는')]
    body = intro + '[1]\n' + '\n'.join(details)
    body += '\n' + '\n'.join(unit + '[1]' for unit in scope_units)
    checked = type(checked)(body, tuple(sorted(set(checked.citation_ids) | {1})),
                            checked.removed_sentences, checked.reasons, checked.replaced_sentences)
    used, source_numbers, mapping = [], {}, {}
    for index in checked.citation_ids:
        article = results[index - 1]
        key = article.get('url') or ('article', index)
        if key not in source_numbers:
            used.append(article)
            source_numbers[key] = len(used)
        mapping[index] = source_numbers[key]
    answer = CITATION.sub(lambda match: f'[{mapping[int(match.group(1))]}]', checked.answer)
    diagnostics.finish(generated_answer=draft, validated_answer=checked.answer,
                       final_answer=answer, validation_reasons=checked.reasons,
                       insufficient_reason=None if used else 'no_supported_sentence_after_validation')
    _log_result(question, answer, used, used[0].get('similarity') if used else None,
                bool(used), int((time.perf_counter() - started) * 1000))
    return AskResponse(answer=answer, sources=[_source(article) for article in used], domain='ent_culture')


def answer_question(question: str, top_k: int) -> AskResponse:
    started = time.perf_counter()
    diagnostics.begin(question)
    analysis, llm_used = _query_analysis(question)
    diagnostics.record(intent=analysis.intent, target_type=analysis.target_type,
                       route='trend_service.aggregate' if analysis.intent == 'trend_ranking' else 'rag',
                       query_analysis=analysis.__dict__, llm_used=llm_used)
    if analysis.intent == 'trend_ranking':
        return _answer_trend(question, analysis, top_k)
    results = _search(question, top_k, analysis)
    diagnostics.record(retrieved_docs=len(results), final_docs=len(results))
    if not results:
        if os.getenv('RAG_DEBUG', '').casefold() == 'true':
            logger.warning('[RAG] question=%s intent=%s entity=%s candidate_docs=0 '
                           'final_docs=0 generated_sentences=0 supported_sentences=0 '
                           'removed_sentences=0 insufficient_reason=no_retrieval_results',
                           question, analysis.intent, analysis.entity or '')
        answer = INSUFFICIENT_ANSWER
        diagnostics.finish(generated_answer='', validated_answer='', final_answer=answer,
                           insufficient_reason='no_retrieval_results')
        _log_result(question, answer, [], None, False, int((time.perf_counter() - started) * 1000))
        return AskResponse(answer=answer, sources=[], domain='ent_culture')

    try:
        context = _context(results)
        draft = generate_answer(question, context)
    except OpenAIServiceError as error:
        raise RAGError(str(error)) from error
    evidence = _evidence(results)
    draft_check = validate_answer(draft, evidence)
    try:
        reviewed = verify_answer(question, context, draft)
        semantic_verified = True
    except OpenAIServiceError:
        # The local validator still protects the draft when semantic review fails.
        reviewed = INSUFFICIENT_ANSWER
        semantic_verified = False
        logger.warning('Answer verification failed; using locally validated draft where possible.')
    checked = validate_answer(reviewed, evidence, semantic_verified=semantic_verified)
    if (not checked.citation_ids and draft_check.citation_ids
            and _has_related_evidence(question, results, analysis)):
        # Recover only verbatim source claims; lexical overlap alone cannot
        # override a semantic rejection (e.g. swapped subject and action).
        checked = validate_answer(draft, evidence, require_extract=True)
        logger.info('Verifier removed all claims; restored locally grounded draft sentences.')
    checked = _title_fallback(question, reviewed, results, checked, analysis)
    used_results = [results[number - 1] for number in checked.citation_ids]
    answer = remap_citations(checked.answer, checked.citation_ids)
    diagnostics.finish(generated_answer=draft, validated_answer=checked.answer,
                       final_answer=answer, cited_docs=len(used_results),
                       validation_reasons=checked.reasons,
                       insufficient_reason=None if used_results else 'no_supported_sentence_after_validation')
    if os.getenv('RAG_DEBUG', '').casefold() == 'true':
        logger.warning('[QUERY] original=%s rule_intent=%s llm_used=%s entity=%s '
                       'intent=%s time_range=%s confidence=%.2f normalized=%s keywords=%s search_queries=%s',
                       question, rule_query_analysis(question).intent, llm_used,
                       analysis.entity, analysis.intent, analysis.time_range,
                       analysis.confidence, analysis.normalized_question, list(analysis.keywords),
                       list(analysis.search_queries))
        expanded_queries = expand_query(question, analysis)
        if analysis.intent == 'definition':
            logger.warning('[definition] question=%s entity=%s expanded_queries=%s '
                           'candidate_count=%d final_docs=%d',
                           question, analysis.entity, list(expanded_queries),
                           len(results), len(used_results))
        logger.warning('[RAG] question=%s intent=%s entity=%s after_filter=%d '
                       'generated_answer=%s generated_sentences=%d validated_sentences=%d removed_sentences=%d',
                       question, analysis.intent, analysis.entity or '', len(results),
                       bool(draft.strip()), len(sentences(draft)), len(checked.citation_ids),
                       len(checked.removed_sentences))
        if not checked.citation_ids:
            logger.warning('[RAG] insufficient_reason=no_supported_sentence_after_validation')
        logger.warning('answer_validation draft_removed=%d final_removed=%d replaced=%d citations=%s',
                       len(draft_check.removed_sentences), len(checked.removed_sentences),
                       len(checked.replaced_sentences), checked.citation_ids)
    _log_result(
        question,
        answer,
        used_results,
        used_results[0]['similarity'] if used_results else None,
        bool(used_results),
        int((time.perf_counter() - started) * 1000),
    )
    return AskResponse(
        answer=answer,
        sources=[_source(article) for article in used_results],
        domain='ent_culture',
    )
