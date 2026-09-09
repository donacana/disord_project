"""Single cosine candidate query, rule-based relevance and metadata selection."""

import logging
import math
import os
import re
import unicodedata
from collections import Counter
from datetime import date, datetime, timezone

if __package__:
    from . import config, db
    from .query_utils import ARTICLE_INTENT_TERMS, QueryAnalysis, QueryHints, analysis_to_hints, analyze_query
else:
    import config
    import db
    from query_utils import ARTICLE_INTENT_TERMS, QueryAnalysis, QueryHints, analysis_to_hints, analyze_query

MIN_SIMILARITY = config.MIN_SIMILARITY
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


def _datetime(value) -> datetime | None:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace('Z', '+00:00'))
        except ValueError:
            return None
    if isinstance(value, date) and not isinstance(value, datetime):
        value = datetime.combine(value, datetime.min.time())
    if not isinstance(value, datetime):
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def recency_score(published_at, now: datetime) -> float:
    published_at = _datetime(published_at)
    if published_at is None:
        return 0.0
    days = max(0, ((_datetime(now) or datetime.now(timezone.utc)) - published_at).days)
    return 1.0 if days <= 7 else 0.7 if days <= 30 else 0.3 if days <= 90 else 0.0


def _coverage(words, text: str) -> float:
    return sum(word in text for word in words) / len(words) if words else 0.0


def score_candidate(row: dict, hints: QueryHints, now: datetime) -> dict | None:
    """Return private ranking features; SourceItem keeps them out of the API."""
    try:
        distance = float(row['distance'])
    except (KeyError, TypeError, ValueError):
        return None
    similarity = 1 - distance
    if not math.isfinite(similarity):
        return None
    title = (row.get('title') or '').casefold()
    body = (row.get('content') or '').casefold()
    direct_text = title + ' ' + body
    keyword_text = direct_text + ' ' + (row.get('summary') or '').casefold()
    keyword = _coverage(hints.keywords, keyword_text)
    title_match = _coverage(hints.keywords, title)
    entity = _coverage(hints.entities, direct_text)
    entity_title = _coverage(hints.entities, title)
    terms = ARTICLE_INTENT_TERMS.get(hints.intent, ())
    title_intent = any(term in title for term in terms)
    body_intent = any(term in body for term in terms)
    intent = 1.0 if title_intent else 0.4 if body_intent else 0.0
    category_name = (row.get('category') or '').strip().casefold()
    category = float(bool(hints.categories) and category_name in hints.categories)
    conflict = (hints.intent in config.POSITIVE_INTENTS and not title_intent
                and any(term in title for term in ARTICLE_INTENT_TERMS['controversy']))
    recency = recency_score(row.get('published_at'), now) if hints.recent_days else 0.0
    published = _datetime(row.get('published_at'))
    in_period = bool(hints.recent_days and published and
                     max(0, (now - published).days) <= hints.recent_days)
    title_entity_bonus = (config.ENTITY_TITLE_BONUS
                          if entity_title == 1.0 and hints.entities else 0.0)
    final = (max(0.0, min(1.0, similarity)) * config.VECTOR_WEIGHT
             + keyword * config.KEYWORD_WEIGHT + title_match * config.TITLE_WEIGHT
             + recency * config.RECENCY_WEIGHT + entity * config.ENTITY_WEIGHT
             + intent * config.INTENT_WEIGHT + category * config.CATEGORY_WEIGHT
             + title_entity_bonus)
    if hints.strict_category and hints.intent != 'definition' and category_name and not category:
        # Strong title evidence softens a potentially incorrect category label.
        final -= config.CATEGORY_MISMATCH_PENALTY * (0.25 if title_intent else 1.0)
    if hints.entities and not entity:
        final -= config.ENTITY_MISMATCH_PENALTY
    if conflict and hints.intent != 'definition':
        final -= config.ACTIVITY_CONFLICT_PENALTY
    return dict(row, distance=distance, similarity=similarity, keyword_score=keyword,
                title_score=title_match, entity_score=entity, entity_title_score=entity_title,
                intent_score=intent, category_score=category, recency_score=recency,
                final_score=max(0.0, min(1.0, final)), in_period=in_period,
                intent_conflict=conflict,
                activity_conflict=conflict and hints.intent == 'activity')


def _eligible(row: dict, hints: QueryHints, min_similarity: float) -> bool:
    effective_min_similarity = max(0.0, min_similarity - config.SIMILARITY_MARGIN)
    # A bounded rescue for exact title/entity evidence; never bypass a configured
    # threshold by more than the margin, and never rescue nonpositive vectors.
    title_rescue = (bool(hints.entities) and row['entity_title_score'] == 1.0
                    and row['similarity'] >= max(config.TITLE_RESCUE_FLOOR,
                                                effective_min_similarity - config.TITLE_RESCUE_MARGIN)
                    and row['final_score'] >= config.ENTITY_SCORE_FLOOR)
    if row['similarity'] < effective_min_similarity and not title_rescue:
        return False
    if row['final_score'] < config.FINAL_SCORE_FLOOR or row['intent_conflict']:
        return False
    if hints.entities:
        if row['entity_score']:
            return row['final_score'] >= config.ENTITY_SCORE_FLOOR
        # Missing entity is not an unconditional veto, but indirect results need
        # strong vector, title intent AND category evidence, and a separate cap.
        return (row['similarity'] >= max(effective_min_similarity, config.INDIRECT_MIN_SIMILARITY)
                and row['intent_score'] == 1.0 and row['category_score'] == 1.0
                and row['final_score'] >= config.INDIRECT_SCORE_FLOOR)
    if hints.intent or hints.categories:
        if hints.intent == 'movie_release' and not row['intent_score']:
            return False
        return bool(row['intent_score'] or row['category_score'] or row['keyword_score'])
    return True


def _normalized_title(title: str) -> str:
    title = unicodedata.normalize('NFKC', title).casefold()
    return re.sub(r'[\W_]+', '', title)


def _deduplicate(rows: list[dict]) -> list[dict]:
    seen_ids, seen_urls, seen_titles = set(), set(), set()
    unique = []
    for row in rows:
        article_id = row.get('article_id')
        url = (row.get('url') or '').strip()
        title = _normalized_title(row.get('title') or '')
        if ((article_id is not None and article_id in seen_ids)
                or (url and url in seen_urls) or (title and title in seen_titles)):
            continue
        unique.append(row)
        if article_id is not None:
            seen_ids.add(article_id)
        if url:
            seen_urls.add(url)
        if title:
            seen_titles.add(title)
    return unique


def _source_key(row: dict) -> str:
    return ' '.join((row.get('source_name') or '').casefold().split())


def _select(rows: list[dict], hints: QueryHints, top_k: int) -> list[dict]:
    def tier(row):
        # Direct relevance outranks time and source diversity. Time filtering is
        # performed only on already eligible rows; old/unknown dates fall back.
        return (int(bool(hints.entities) and not row['entity_score']),
                int(bool(hints.recent_days) and not row['in_period']))

    def order(row):
        collected = _datetime(row.get('collected_at'))
        return (*tier(row), -row['final_score'], -row['similarity'],
                -(collected.timestamp() if collected else 0))

    remaining = _deduplicate(sorted(rows, key=order))
    selected, counts = [], Counter()
    direct_count = sum(bool(row['entity_score']) for row in remaining)
    # At most one indirect result, and never a majority when direct ones exist.
    indirect_limit = min(1, max(0, direct_count - 1)) if direct_count else 1
    indirect_count = 0
    while remaining and len(selected) < top_k:
        best = remaining[0]
        if hints.entities and not best['entity_score'] and indirect_count >= indirect_limit:
            remaining.pop(0)
            continue
        index = 0
        if counts[_source_key(best)] >= config.SOURCE_LIMIT:
            for candidate_index, candidate in enumerate(remaining[1:], start=1):
                if tier(candidate) != tier(best):
                    break
                if best['final_score'] - candidate['final_score'] > config.DIVERSITY_SCORE_GAP:
                    break
                if counts[_source_key(candidate)] < config.SOURCE_LIMIT:
                    index = candidate_index
                    break
        chosen = remaining.pop(index)
        selected.append(chosen)
        counts[_source_key(chosen)] += 1
        if hints.entities and not chosen['entity_score']:
            indirect_count += 1
    return selected


def rank_candidates(rows: list[dict], question: str, top_k: int,
                    min_similarity: float = MIN_SIMILARITY,
                    now: datetime | None = None,
                    analysis: QueryAnalysis | None = None) -> list[dict]:
    hints = analysis_to_hints(analysis) if analysis else analyze_query(question)
    now = _datetime(now) or datetime.now(timezone.utc)
    eligible = []
    debug = os.getenv('RAG_DEBUG', '').casefold() == 'true'
    for original in rows:
        row = score_candidate(original, hints, now)
        if row is None:
            continue
        kept = _eligible(row, hints, min_similarity)
        if debug:
            logger.warning('article_id=%s title=%r vector=%.3f entity=%.3f intent=%.3f '
                           'category=%.3f recency=%.3f final=%.3f source_name=%r eligible=%s',
                           row.get('article_id'), (row.get('title') or '')[:80], row['similarity'],
                           row['entity_score'], row['intent_score'], row['category_score'],
                           row['recency_score'], row['final_score'], row.get('source_name'), kept)
        if kept:
            eligible.append(row)
    return _select(eligible, hints, top_k)


def search(embedding: list[float], question: str, top_k: int,
           min_similarity: float = MIN_SIMILARITY,
           analysis: QueryAnalysis | None = None) -> list[dict]:
    vector = '[' + ','.join(str(value) for value in embedding) + ']'
    candidate_count = min(config.MAX_CANDIDATES,
                          max(config.CANDIDATE_TOP_K, top_k * config.CANDIDATE_MULTIPLIER))
    rows = db.fetch_all(SEARCH_QUERY, (vector, vector, candidate_count))
    ranked = rank_candidates(rows, question, top_k, min_similarity, analysis=analysis)
    if os.getenv('RAG_DEBUG', '').casefold() == 'true':
        logger.warning('[RAG] question=%s vector_candidates=%d after_rerank=%d final_docs=%d',
                       question, len(rows), len(ranked), len(ranked))
    return ranked
