import json
import logging
import os
import time
from datetime import datetime

if __package__:
    from . import config
    from . import db
    from .openai_client import (OpenAIServiceError, embed_question, generate_answer,
                                generate_trend_answer, verify_answer)
    from .answer_validator import CITATION, INSUFFICIENT_ANSWER, remap_citations, validate_answer
    from .query_utils import (ARTICLE_INTENT_TERMS, QueryAnalysis, analyze_query,
                               expand_query, merge_llm_analysis, rule_query_analysis,
                               should_use_llm)
    from .retrieval import MIN_SIMILARITY, search
    from . import trend_service
    from .schemas import AskResponse, SourceItem
else:
    import config
    import db
    from openai_client import (OpenAIServiceError, embed_question, generate_answer,
                               generate_trend_answer, verify_answer)
    from answer_validator import CITATION, INSUFFICIENT_ANSWER, remap_citations, validate_answer
    from query_utils import (ARTICLE_INTENT_TERMS, QueryAnalysis, analyze_query,
                             expand_query, merge_llm_analysis, rule_query_analysis,
                             should_use_llm)
    from retrieval import MIN_SIMILARITY, search
    import trend_service
    from schemas import AskResponse, SourceItem


MAX_CONTEXT_CHARS = 4000
logger = logging.getLogger(__name__)


class RAGError(RuntimeError):
    """Raised when embedding or answer generation fails."""


def _query_analysis(question: str) -> tuple[QueryAnalysis, bool]:
    rule = rule_query_analysis(question)
    if not should_use_llm(rule):
        return rule, False
    try:
        from .openai_client import analyze_question as llm_analyze_question
    except ImportError:
        from openai_client import analyze_question as llm_analyze_question
    try:
        return merge_llm_analysis(rule, llm_analyze_question(question)), True
    except OpenAIServiceError:
        logger.warning('Query understanding failed; using rule-based analysis.')
        return rule, False


def _search(question: str, top_k: int, analysis: QueryAnalysis | None = None) -> list[dict]:
    try:
        analysis = analysis or _query_analysis(question)[0]
        embedding = embed_question(' | '.join(expand_query(question, analysis)))
        return search(embedding, question, top_k, MIN_SIMILARITY, analysis=analysis)
    except (db.DatabaseError, OpenAIServiceError) as error:
        raise RAGError(str(error)) from error

def _article_body(article: dict) -> str:
    return (article['summary'] or article['content'] or '')[:MAX_CONTEXT_CHARS]


def _evidence(results: list[dict]) -> list[str]:
    # No unseen full content, URLs or publication dates can substantiate a
    # factual claim. Validate against the exact title/body sent to the model.
    return [f'{article["title"]}\n{_article_body(article)}' for article in results]


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

    Never override the verifier's explicit insufficiency verdict, and never
    infer performances/releases from a generic collaboration headline.
    """
    hints = analyze_query(question)
    if analysis and analysis.entity:
        hints = type(hints)(hints.keywords, (analysis.entity,), hints.intent, hints.categories,
                            hints.strict_category, hints.recent_days)
    if checked.citation_ids or (reviewed.strip() == INSUFFICIENT_ANSWER
                                and hints.intent != 'definition'):
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
    question = analysis.original_question.casefold()
    if any(term in question for term in ('아이돌', '걸그룹', '보이그룹', '그룹')):
        return 'idol'
    if any(term in question for term in ('배우', '연기자')):
        return 'actor'
    return None


def _trend_articles(items: list[trend_service.TrendItem]) -> list[dict]:
    articles = []
    for item in items:
        representative = item.articles[0] if item.articles else {}
        article = dict(representative)
        article['title'] = representative.get('title') or item.name
        article['content'] = (f'집계 대상: {item.name}. '
                              f'최근 수집 기사 {item.mention_count}건, '
                              f'언론사 {item.source_count}곳, '
                              f'최신 기사 {item.latest_at.isoformat() if item.latest_at else "미상"}.')
        article['summary'] = None
        article['url'] = representative.get('url') or f'trend://{item.name}'
        article['collected_at'] = representative.get('collected_at')
        article['similarity'] = item.score
        articles.append(article)
    return articles


def _answer_trend(question: str, analysis: QueryAnalysis, top_k: int) -> AskResponse:
    now = datetime.now().astimezone()
    items, scanned = trend_service.aggregate(
        analysis.time_range,
        top_k=min(top_k, config.TREND_TOP_K),
        category_hint=_trend_category_hint(analysis),
        now=now,
    )
    if not items:
        answer = INSUFFICIENT_ANSWER
        _log_result(question, answer, [], None, False, 0)
        return AskResponse(answer=answer, sources=[], domain='ent_culture')
    results = _trend_articles(items)
    context = '\n'.join(
        f'[{index}] {item.name}: 기사 {item.mention_count}건, 언론사 {item.source_count}곳, '
        f'최신일 {item.latest_at.date().isoformat() if item.latest_at else "미상"}'
        for index, item in enumerate(items, start=1)
    )
    try:
        draft = generate_trend_answer(question, context)
    except OpenAIServiceError:
        draft = ('최근 수집 기사 기준으로는 ' + ', '.join(
            f'{item.name}({item.mention_count}건)' for item in items
        ) + '이(가) 많이 언급됐습니다.' + ''.join(
            f'[{index}]' for index in range(1, len(items) + 1)
        ))
    evidence = [
        f'최근 수집 기사 기준으로 {item.name}이(가) {item.mention_count}건으로 집계됐습니다. '
        f'{item.name}: 기사 {item.mention_count}건, 언론사 {item.source_count}곳, '
        f'최신일 {item.latest_at.date().isoformat() if item.latest_at else "미상"}\n{article["title"]}'
        for item, article in zip(items, results)
    ]
    checked = validate_answer(draft, evidence)
    if not checked.citation_ids:
        fallback = '\n'.join(
            f'최근 수집 기사 기준으로 {item.name}이(가) {item.mention_count}건으로 집계됐습니다.[{index}]'
            for index, item in enumerate(items, start=1)
        )
        checked = validate_answer(fallback, evidence)
    used = [results[index - 1] for index in checked.citation_ids]
    answer = remap_citations(checked.answer, checked.citation_ids)
    if os.getenv('RAG_DEBUG', '').casefold() == 'true':
        logger.warning('[TREND] intent=%s time_range=%s period_days=%d articles_scanned=%d '
                       'entities_found=%d eligible_entities=%d top_entities=%s',
                       analysis.intent, analysis.time_range,
                       trend_service.period_days(analysis.time_range), scanned,
                       len(items), len(used),
                       [(item.name, item.mention_count, item.source_count, round(item.score, 3))
                        for item in items])
    _log_result(question, answer, used, used[0].get('similarity') if used else None,
                bool(used), 0)
    return AskResponse(answer=answer, sources=[_source(article) for article in used], domain='ent_culture')


def answer_question(question: str, top_k: int) -> AskResponse:
    started = time.perf_counter()
    analysis, llm_used = _query_analysis(question)
    if analysis.intent == 'trend_ranking':
        return _answer_trend(question, analysis, top_k)
    results = _search(question, top_k, analysis)
    if not results:
        answer = INSUFFICIENT_ANSWER
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
    except OpenAIServiceError:
        # Never return an unverified draft on verifier failure.
        reviewed = INSUFFICIENT_ANSWER
        logger.warning('Answer verification failed; returning insufficient evidence response.')
    checked = validate_answer(reviewed, evidence)
    checked = _title_fallback(question, reviewed, results, checked, analysis)
    used_results = [results[number - 1] for number in checked.citation_ids]
    answer = remap_citations(checked.answer, checked.citation_ids)
    if os.getenv('RAG_DEBUG', '').casefold() == 'true':
        logger.warning('[QUERY] original=%s rule_intent=%s llm_used=%s entity=%s '
                       'intent=%s time_range=%s confidence=%.2f normalized=%s keywords=%s',
                       question, rule_query_analysis(question).intent, llm_used,
                       analysis.entity, analysis.intent, analysis.time_range,
                       analysis.confidence, analysis.normalized_question, list(analysis.keywords))
        expanded_queries = expand_query(question, analysis)
        if analysis.intent == 'definition':
            logger.warning('[definition] question=%s entity=%s expanded_queries=%s '
                           'candidate_count=%d final_docs=%d',
                           question, analysis.entity, list(expanded_queries),
                           len(results), len(used_results))
        logger.warning('[RAG] question=%s intent=%s entity=%s after_filter=%d '
                       'generated_answer=%s validated_sentences=%d removed_sentences=%d',
                       question, analysis.intent, analysis.entity or '', len(results),
                       bool(draft.strip()), len(checked.citation_ids),
                       len(checked.removed_sentences))
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
