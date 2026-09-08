import logging

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse

if __package__:
    from . import db
    from .schemas import AskRequest, AskResponse
    from .rag import RAGError, answer_question
else:
    import db
    from schemas import AskRequest, AskResponse
    from rag import RAGError, answer_question

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
        logger.warning('Health check database error: %s; original=%r', error, error.__cause__ or error)
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


@app.post('/ask', response_model=AskResponse)
def ask(request: AskRequest):
    try:
        return answer_question(request.question, request.top_k)
    except (db.DatabaseError, RAGError) as error:
        logger.warning('%s', error)
        raise HTTPException(status_code=503, detail=str(error)) from None
