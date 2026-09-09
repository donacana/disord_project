import json
import logging
import os
import time
from datetime import datetime

if __package__:
    from . import db
    from .openai_client import OpenAIServiceError, embed_question, generate_answer, verify_answer
    from .answer_validator import CITATION, INSUFFICIENT_ANSWER, remap_citations, validate_answer
    from .query_utils import ARTICLE_INTENT_TERMS, analyze_query
    from .retrieval import MIN_SIMILARITY, search
    from .schemas import AskResponse, SourceItem
else:
    import db
    from openai_client import OpenAIServiceError, embed_question, generate_answer, verify_answer
    from answer_validator import CITATION, INSUFFICIENT_ANSWER, remap_citations, validate_answer
    from query_utils import ARTICLE_INTENT_TERMS, analyze_query
    from retrieval import MIN_SIMILARITY, search
    from schemas import AskResponse, SourceItem


MAX_CONTEXT_CHARS = 4000
logger = logging.getLogger(__name__)


class RAGError(RuntimeError):
    """Raised when embedding or answer generation fails."""


def _search(question: str, top_k: int) -> list[dict]:
    try:
        embedding = embed_question(question)
        return search(embedding, question, top_k, MIN_SIMILARITY)
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


def _title_fallback(question: str, reviewed: str, results: list[dict], checked):
    """Keep a directly relevant headline when an over-detailed review is pruned.

    Never override the verifier's explicit insufficiency verdict, and never
    infer performances/releases from a generic collaboration headline.
    """
    if checked.citation_ids or reviewed.strip() == INSUFFICIENT_ANSWER or 'global_insufficiency' in checked.reasons:
        return checked
    hints = analyze_query(question)
    terms = ARTICLE_INTENT_TERMS.get(hints.intent, ())
    if not terms:
        return checked
    lines = []
    for number in sorted(set(map(int, CITATION.findall(reviewed)))):
        if not 1 <= number <= len(results):
            continue
        title = results[number - 1]['title']
        folded = title.casefold()
        if (not any(term in folded for term in terms)
                or any(entity not in folded for entity in hints.entities)):
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
    results = _search(question, top_k)
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
    checked = validate_answer(reviewed, evidence, require_extract=True)
    checked = _title_fallback(question, reviewed, results, checked)
    used_results = [results[number - 1] for number in checked.citation_ids]
    answer = remap_citations(checked.answer, checked.citation_ids)
    if os.getenv('RAG_DEBUG', '').casefold() == 'true':
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
