"""Cosine candidate retrieval followed by normalized hybrid scoring."""

import logging
import math
import os
from datetime import date, datetime, timezone

if __package__:
    from . import db
    from .query_utils import extract_keywords, wants_recent
else:
    import db
    from query_utils import extract_keywords, wants_recent

VECTOR_WEIGHT = 0.60
KEYWORD_WEIGHT = 0.20
TITLE_WEIGHT = 0.15
RECENCY_WEIGHT = 0.05
MIN_SIMILARITY = float(os.getenv('RAG_MIN_SIMILARITY', '0.3'))
CANDIDATE_MULTIPLIER = 4
MAX_CANDIDATES = 40
logger = logging.getLogger(__name__)

SEARCH_QUERY = """
SELECT a.id AS article_id, a.title, a.content, a.summary, a.url,
       a.source_name, a.category, a.published_at, a.collected_at,
       ae.embedding <=> %s::vector AS distance
FROM public.article_embeddings ae
JOIN public.articles a ON a.id = ae.article_id
WHERE ae.embedding IS NOT NULL
ORDER BY ae.embedding <=> %s::vector
LIMIT %s
"""


def recency_score(published_at, now: datetime) -> float:
    if published_at is None:
        return 0.0
    if isinstance(published_at, str):
        try:
            published_at = datetime.fromisoformat(published_at.replace('Z', '+00:00'))
        except ValueError:
            return 0.0
    if isinstance(published_at, date) and not isinstance(published_at, datetime):
        published_at = datetime.combine(published_at, datetime.min.time())
    if not isinstance(published_at, datetime):
        return 0.0
    if published_at.tzinfo is None:
        published_at = published_at.replace(tzinfo=timezone.utc)
    days = max(0, (now - published_at).days)
    return 1.0 if days <= 7 else 0.7 if days <= 30 else 0.3 if days <= 90 else 0.0


def rank_candidates(rows: list[dict], question: str, top_k: int,
                    min_similarity: float = MIN_SIMILARITY,
                    now: datetime | None = None) -> list[dict]:
    keywords = extract_keywords(question)
    recent = wants_recent(question)
    now = now or datetime.now(timezone.utc)
    ranked = []
    debug = os.getenv('RAG_DEBUG', '').casefold() == 'true'
    for row in rows:
        if row.get('distance') is None:
            continue
        distance = float(row['distance'])
        similarity = 1 - distance
        if not math.isfinite(similarity):
            continue
        title = (row.get('title') or '').casefold()
        text = ' '.join((title, row.get('content') or '', row.get('summary') or '')).casefold()
        keyword = sum(word in text for word in keywords) / len(keywords) if keywords else 0.0
        title_match = sum(word in title for word in keywords) / len(keywords) if keywords else 0.0
        recency = recency_score(row.get('published_at'), now) if recent else 0.0
        final = (max(0.0, min(1.0, similarity)) * VECTOR_WEIGHT
                 + keyword * KEYWORD_WEIGHT + title_match * TITLE_WEIGHT
                 + recency * RECENCY_WEIGHT)
        if debug:
            logger.warning('article_id=%s vector=%.3f keyword=%.3f title=%.3f recency=%.3f final=%.3f kept=%s',
                           row['article_id'], similarity, keyword, title_match, recency,
                           final, similarity >= min_similarity)
        # Threshold always applies to cosine similarity, not the boosted score.
        if similarity >= min_similarity:
            ranked.append(dict(row, distance=distance, similarity=similarity, final_score=final))
    ranked.sort(key=lambda row: (row['final_score'], row['similarity']), reverse=True)
    return ranked[:top_k]


def search(embedding: list[float], question: str, top_k: int,
           min_similarity: float = MIN_SIMILARITY) -> list[dict]:
    vector = '[' + ','.join(str(value) for value in embedding) + ']'
    candidate_count = min(MAX_CANDIDATES, top_k * CANDIDATE_MULTIPLIER)
    rows = db.fetch_all(SEARCH_QUERY, (vector, vector, candidate_count))
    return rank_candidates(rows, question, top_k, min_similarity)
