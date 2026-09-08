import logging

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse

if __package__:
    from . import db
    from .schemas import AskRequest, AskResponse, SourceItem
else:
    import db
    from schemas import AskRequest, AskResponse, SourceItem

app = FastAPI(title='Entertainment Culture RAG API')
logger = logging.getLogger(__name__)

STATS_QUERY = """
SELECT
    (SELECT COUNT(*) FROM public.articles) AS total_articles,
    (SELECT COUNT(*) FROM public.article_embeddings) AS total_embeddings,
    (SELECT MAX(collected_at) FROM public.articles) AS last_collected_at,
    (SELECT COUNT(*) FROM public.sources) AS source_count
"""


@app.get('/')
def root():
    return {'message': 'Entertainment Culture RAG API'}


@app.get('/health', responses={503: {'description': 'Database unavailable'}})
def health():
    try:
        db.fetch_one('SELECT 1 AS ok')
    except db.DatabaseError as error:
        logger.warning('%s', error)
        return JSONResponse(status_code=503, content={
            'status': 'error', 'api': 'ok', 'database': 'error',
        })
    return {'status': 'ok', 'api': 'ok', 'database': 'ok'}


@app.get('/stats', responses={503: {'description': 'Database unavailable'}})
def stats():
    try:
        return db.fetch_one(STATS_QUERY)
    except db.DatabaseError as error:
        logger.warning('%s', error)
        raise HTTPException(status_code=503, detail=str(error)) from None


def search_articles(question: str, top_k: int) -> list[SourceItem]:
    """Placeholder: no database search or embedding calls yet."""
    return []


def generate_answer(question: str, sources: list[SourceItem]) -> str:
    """Placeholder: no LLM calls yet."""
    return 'RAG 검색 기능은 아직 연결되지 않았습니다.'


def handle_ask(request: AskRequest) -> AskResponse:
    sources = search_articles(request.question, request.top_k)
    return AskResponse(
        answer=generate_answer(request.question, sources),
        sources=sources,
        domain='ent_culture',
    )


@app.post('/ask', response_model=AskResponse)
def ask(request: AskRequest):
    return handle_ask(request)
