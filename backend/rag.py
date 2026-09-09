import json
import time
from datetime import datetime

if __package__:
    from . import db
    from .openai_client import OpenAIServiceError, embed_question, generate_answer
    from .retrieval import MIN_SIMILARITY, search
    from .schemas import AskResponse, SourceItem
else:
    import db
    from openai_client import OpenAIServiceError, embed_question, generate_answer
    from retrieval import MIN_SIMILARITY, search
    from schemas import AskResponse, SourceItem


MAX_CONTEXT_CHARS = 4000


class RAGError(RuntimeError):
    """Raised when embedding or answer generation fails."""


def _search(question: str, top_k: int) -> list[dict]:
    try:
        embedding = embed_question(question)
        return search(embedding, question, top_k, MIN_SIMILARITY)
    except (db.DatabaseError, OpenAIServiceError) as error:
        raise RAGError(str(error)) from error

def _context(results: list[dict]) -> str:
    sections = []
    for index, article in enumerate(results, start=1):
        published_at = article['published_at'] or '미상'
        body = (article['summary'] or article['content'] or '')[:MAX_CONTEXT_CHARS]
        sections.append(
            f'[{index}]\n'
            f'제목: {article["title"]}\n'
            f'출처: {article["source_name"]}\n'
            f'발행일: {published_at}\n'
            f'내용: {body}\n'
            f'URL: {article["url"]}'
        )
    return '\n\n'.join(sections)


def _source(article: dict) -> SourceItem:
    return SourceItem(
        title=article['title'],
        url=article['url'],
        collected_at=article['collected_at'],
    )


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
        answer = '현재 수집된 자료에서 관련 정보를 찾지 못했습니다.'
        _log_result(question, answer, [], None, False, int((time.perf_counter() - started) * 1000))
        return AskResponse(answer=answer, sources=[], domain='ent_culture')

    try:
        answer = generate_answer(question, _context(results))
    except OpenAIServiceError as error:
        raise RAGError(str(error)) from error
    _log_result(
        question,
        answer,
        results,
        results[0]['similarity'],
        True,
        int((time.perf_counter() - started) * 1000),
    )
    return AskResponse(
        answer=answer,
        sources=[_source(article) for article in results],
        domain='ent_culture',
    )
