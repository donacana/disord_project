import json
import logging
import os
import time
from datetime import datetime

if __package__:
    from . import db
    from .openai_client import OpenAIServiceError, embed_question, generate_answer, verify_answer
    from .answer_validator import CITATION, INSUFFICIENT_ANSWER, remap_citations, validate_answer
    from .query_utils import (ARTICLE_INTENT_TERMS, QueryAnalysis, analyze_query,
                               expand_query, merge_llm_analysis, rule_query_analysis,
                               should_use_llm)
    from .retrieval import MIN_SIMILARITY, search
    from .schemas import AskResponse, SourceItem
else:
    import db
    from openai_client import OpenAIServiceError, embed_question, generate_answer, verify_answer
    from answer_validator import CITATION, INSUFFICIENT_ANSWER, remap_citations, validate_answer
    from query_utils import (ARTICLE_INTENT_TERMS, QueryAnalysis, analyze_query,
                             expand_query, merge_llm_analysis, rule_query_analysis,
                             should_use_llm)
    from retrieval import MIN_SIMILARITY, search
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


def answer_question(question: str, top_k: int) -> AskResponse:
    started = time.perf_counter()
    analysis, llm_used = _query_analysis(question)
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
