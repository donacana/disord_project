"""Database-backed trend ranking without changing the public API contract."""

import logging
import re
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from threading import Lock
from urllib.parse import urlsplit
from datetime import date, datetime, timedelta, timezone

if __package__:
    from . import config, db, diagnostics, entity_cache
    from .openai_client import OpenAIServiceError, extract_trend_entities
    from .query_utils import ENTITY_ALIASES
else:
    import config
    import db
    import diagnostics
    import entity_cache
    from openai_client import OpenAIServiceError, extract_trend_entities
    from query_utils import ENTITY_ALIASES

logger = logging.getLogger(__name__)
_extraction_lock = Lock()

TREND_ARTICLES_QUERY = """
SELECT id AS article_id, title, content, category, source_name, url,
       url_hash, published_at, collected_at
FROM public.articles
WHERE published_at >= %s
  AND published_at <= %s
  AND (title IS NOT NULL OR content IS NOT NULL)
ORDER BY published_at DESC
LIMIT %s
"""

GENERIC = {
    '최근', '요즘', '오늘', '뉴스', '소식', '활동', '컴백', '앨범', '신곡', '발매',
    '공연', '방송', '예능', '드라마', '영화', '배우', '가수', '아이돌', '그룹',
    '걸그룹', '보이그룹', '화제', '인기', '논란', '사건', '공개', '확정', '출연',
    '팬미팅', '콘서트', '행사', '서울', '한국', '세계', '기자', '사진', '영상',
    '소속사', '멤버', '제작', '작품', '문화', '연예', '스타', '무대', '신작',
    '일정', '시리즈', '화합', '대전', '대구', '서울', '부산', '인천', '광주',
    '경기', '전국', '전시', '축제', '센터', '협회', '기업', '시장', '정부',
}
ENTITY_PATTERN = re.compile(r'[가-힣A-Za-z][가-힣A-Za-z0-9&+·.-]{1,15}')
QUOTED_PATTERN = re.compile(r'["\'“‘「『]([^"\'”’」』]{2,80})["\'”’」』]')


@dataclass(frozen=True)
class TrendItem:
    name: str
    mention_count: int
    source_count: int
    latest_at: datetime | None
    score: float
    articles: tuple[dict, ...]
    candidate_type: str = 'entertainer'


def period_days(time_range: str) -> int:
    return {
        'today': 1,
        'week': 7,
        'recent': config.TREND_DEFAULT_DAYS,
        'month': config.TREND_MONTH_DAYS,
    }.get(time_range, config.TREND_DEFAULT_DAYS)


def period_start(time_range: str, now: datetime | None = None) -> datetime:
    now = now or datetime.now(timezone.utc)
    if time_range == 'year':
        return datetime(now.year, 1, 1, tzinfo=timezone.utc)
    return now - timedelta(days=period_days(time_range))


def _as_utc(value) -> datetime | None:
    if isinstance(value, date) and not isinstance(value, datetime):
        value = datetime.combine(value, datetime.min.time())
    if not isinstance(value, datetime):
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _normalize(name: str) -> str:
    return re.sub(r'\s+', ' ', name).strip(' .,:;!?()[]{}<>"\'“”‘’')


def extract_candidates(title: str) -> list[str]:
    """Extract likely person/group names from titles; no per-article LLM calls."""
    candidates = []
    for name in QUOTED_PATTERN.findall(title or ''):
        name = _normalize(name)
        if name not in GENERIC and len(name) <= 12:
            candidates.append(name)
    head = re.split(r'[,|:…]', title or '', maxsplit=1)[0]
    for name in ENTITY_PATTERN.findall(head):
        name = _normalize(name)
        if name.casefold() in {word.casefold() for word in GENERIC}:
            continue
        if re.fullmatch(r'\d+', name) or name in {'관련', '대상', '기자'}:
            continue
        candidates.append(name)
    unique = []
    seen = set()
    for name in candidates:
        key = name.casefold()
        if key not in seen:
            seen.add(key)
            unique.append(name)
    return unique


WORK_MARKERS = {
    'song': ('신곡', '타이틀곡', '싱글', 'OST', '수록곡', '음원', '곡', '발매', '차트', '컴백'),
    'movie': ('영화', '개봉', '박스오피스', '관객', '시사회', '감독', '작품', '흥행'),
    'drama': ('드라마', '시리즈', '시즌', '방영', 'OTT', '방송', '작품'),
}


def _work_context(text: str, marker: str, value: str) -> bool:
    start = max(0, text.casefold().find(value.casefold()) - 100)
    end = min(len(text), start + len(value) + 100)
    window = text[start:end].casefold()
    return marker.casefold() in window


def _extract_work_candidates(text: str, target: str) -> list[str]:
    markers = WORK_MARKERS[target]
    candidates = []
    for match in QUOTED_PATTERN.finditer(text or ''):
        value = _normalize(match.group(1))
        context = text[max(0, match.start() - 100):min(len(text), match.end() + 100)]
        if value.casefold() in {word.casefold() for word in GENERIC}:
            continue
        if any(marker.casefold() in context.casefold() for marker in markers):
            candidates.append(value)
    return list(dict.fromkeys(candidates))


def extract_song_candidates(title: str, content: str = '') -> list[str]:
    return _extract_work_candidates(f'{title}\n{content}', 'song')


def extract_movie_candidates(title: str, content: str = '') -> list[str]:
    return _extract_work_candidates(f'{title}\n{content}', 'movie')


def extract_drama_candidates(title: str, content: str = '') -> list[str]:
    return _extract_work_candidates(f'{title}\n{content}', 'drama')


def _passage(row: dict) -> str:
    body = re.sub(r'\s+', ' ', row.get('content') or '')
    roles = [match.group() for match in re.finditer(
        r'[^.!?]{0,30}(?:배우|아이돌|그룹|가수|방송인)[^.!?]{0,120}', body)]
    return (row.get('title') or '') + '\n' + (' '.join(roles[:3]) + '\n' + body[:200])[:600]


def _extract_batch(passages: tuple[str, ...]) -> tuple[tuple[str, str, int], ...]:
    context = '\n\n'.join(f'ARTICLE {i}\n{text}' for i, text in enumerate(passages))
    found = []
    for item in extract_trend_entities(context):
        index, name = item.get('article_index'), item.get('name')
        quote = item.get('evidence_quote', name)
        kind = item.get('target_type')
        if (isinstance(index, int) and 0 <= index < len(passages)
                and isinstance(name, str) and len(name.strip()) >= 2
                and isinstance(quote, str) and name in quote and quote in passages[index]
                and kind in {'idol_or_group', 'actor', 'entertainer'}):
            if name in GENERIC:
                continue
            if kind == 'idol_or_group' and not re.search(r'아이돌|그룹|멤버', passages[index]):
                kind = 'entertainer'
            found.append((name.strip(), kind, index))
    return tuple(dict.fromkeys(found))


def _explicit_entities(passages: list[str]) -> list[tuple[str, str]]:
    """Conservative fallback: require an explicit role adjacent to a name."""
    found = []
    for passage in passages:
        title = passage.split('\n', 1)[0]
        match = re.match(r'^(?:\[[^\]]+\]\s*)?([가-힣A-Za-z&][가-힣A-Za-z0-9& ]{1,20}),', title)
        if not match:
            continue
        name = match.group(1).strip()
        if name in GENERIC:
            continue
        # A headline subject plus an explicit entertainment role in the body
        # avoids classifying arbitrary headline tokens as people on API failure.
        role = re.search(r'(배우|가수|아이돌|보이그룹|걸그룹)\s+[‘\'"]?' + re.escape(name)
                         + r'(?=$|[^가-힣A-Za-z]|은|는|이|가|의)', passage)
        if role:
            found.append((name, 'actor' if role[1] == '배우' else 'entertainer' if role[1] == '가수' else 'idol_or_group'))
    return list(dict.fromkeys(found))


def _has_name(text: str, name: str) -> bool:
    # Do not count short names embedded in other names or words.
    return bool(re.search(r'(?<![가-힣A-Za-z0-9])' + re.escape(name)
                         + r'(?=$|[^가-힣A-Za-z0-9]|은|는|이|가|을|를|의|와|과|도)', text, re.IGNORECASE))


def _article_mentions(passages: list[str]) -> list[list[tuple[str, str]]]:
    """Call only while holding the extraction lock to coalesce concurrent misses."""
    keys = [entity_cache.key(passage) for passage in passages]
    cached = entity_cache.get_many(keys)
    missing = dict((key, passage) for key, passage in zip(keys, passages) if key not in cached)
    missing_keys = list(missing)
    batches = [missing_keys[i:i + config.TREND_ENTITY_BATCH_SIZE]
               for i in range(0, len(missing_keys), config.TREND_ENTITY_BATCH_SIZE)]
    diagnostics.record(entity_cache_hits=sum(key in cached for key in keys),
                       entity_cache_misses=len(missing), entity_llm_batches=len(batches))
    failures = 0
    with ThreadPoolExecutor(max_workers=config.TREND_ENTITY_WORKERS) as pool:
        futures = [pool.submit(_extract_batch, tuple(missing[key] for key in batch)) for batch in batches]
        for batch, future in zip(batches, futures):
            extracted = {key: ([], True) for key in batch}
            try:
                for name, kind, index in future.result():
                    extracted[batch[index]][0].append((name, kind))
            except OpenAIServiceError:
                failures += 1
                extracted = {key: (_explicit_entities([missing[key]]), False) for key in batch}
            # Store each completed batch immediately; a later failure or process
            # restart must not discard all of the successful article analyses.
            entity_cache.put_many(extracted)
            cached.update(extracted)
    diagnostics.record(entity_extraction_failures=failures,
                       entity_extraction_incomplete_articles=sum(not cached[key][1] for key in keys))
    return [list(cached[key][0]) for key in keys]


def _entity_rows(rows: list[dict], category_hint: str | None) -> list[dict]:
    passages = [_passage(row) for row in rows]
    with _extraction_lock:
        mentions = _article_mentions(passages)
    catalog = [item for article_mentions in mentions for item in article_mentions]
    desired = {'idol': 'idol_or_group', 'actor': 'actor'}.get(category_hint)
    eligible = {(name, kind) for name, kind in catalog if not desired or kind == desired}
    if not eligible:
        for index, passage in enumerate(passages):
            mentions[index].extend(_explicit_entities([passage]))
        catalog = [item for article_mentions in mentions for item in article_mentions]
        eligible = {(name, kind) for name, kind in catalog if not desired or kind == desired}
    diagnostics.record(raw_entities=len({name for name, _ in catalog}),
                       eligible_entities=len({name for name, _ in eligible}),
                       raw_candidates=[{'name': name, 'candidate_type': kind,
                                        'context': 'person_or_group'} for name, kind in catalog],
                       validated_candidates=[{'name': name, 'candidate_type': kind,
                                              'context': 'person_or_group'} for name, kind in eligible])
    diagnostics.record(extractor_used=(
        'extract_idol_group_candidates' if category_hint in {'idol', 'group'}
        else 'extract_person_candidates'))
    eligible_names = {ENTITY_ALIASES.get(name.casefold(), name) for name, _ in eligible}
    output = []
    for row, article_mentions in zip(rows, mentions):
        # A role established in one DB article applies to recognized mentions
        # in other articles too, even when those do not repeat the role label.
        names = {ENTITY_ALIASES.get(name.casefold(), name)
                 for name, _ in article_mentions
                 if ENTITY_ALIASES.get(name.casefold(), name) in eligible_names}
        output.append({**row, '_trend_entities': sorted(names)})
    return output


def extract_person_candidates(rows: list[dict], category_hint: str | None = None) -> list[dict]:
    return _entity_rows(rows, category_hint)


def extract_idol_group_candidates(rows: list[dict]) -> list[dict]:
    return _entity_rows(rows, 'idol')


def _work_entity_rows(rows: list[dict], target_type: str) -> list[dict]:
    extractor = {
        'song': extract_song_candidates,
        'movie': extract_movie_candidates,
        'drama': extract_drama_candidates,
    }[target_type]
    output = []
    raw = set()
    for row in rows:
        names = extractor(row.get('title') or '', row.get('content') or '')
        raw.update(names)
        output.append({**row, '_trend_entities': names,
                       '_trend_candidate_types': {name: target_type for name in names}})
    diagnostics.record(raw_entities=len(raw), eligible_entities=len(raw),
                       extractor_used=f'extract_{target_type}_candidates',
                       raw_candidates=[{'name': name, 'candidate_type': target_type,
                                        'context': target_type} for name in sorted(raw)],
                       validated_candidates=[{'name': name, 'candidate_type': target_type,
                                              'context': target_type} for name in sorted(raw)])
    return output


def _source_key(row: dict) -> str:
    # Aggregators (e.g. 네이버뉴스) are not the publisher. Count URL origins
    # consistently for direct feeds and aggregator-collected articles alike.
    try:
        host = (urlsplit(row.get('url') or '').hostname or '').casefold()
    except ValueError:
        host = ''
    for prefix in ('www.', 'm.', 'mobile.'):
        if host.startswith(prefix):
            host = host[len(prefix):]
    return host or (row.get('source_name') or '').strip().casefold()


def _category_allowed(row: dict, category_hint: str | None) -> bool:
    category = (row.get('category') or '').casefold()
    if not category_hint:
        return category != 'event'
    if category_hint in {'idol', 'group', 'singer'}:
        return category in {'music', 'celeb', 'event', 'trend'}
    if category_hint in {'배우', 'actor'}:
        return category in {'celeb', 'drama', 'movie', 'trend'}
    return True


def aggregate_rows(rows: list[dict], now: datetime, top_k: int = config.TREND_TOP_K,
                   category_hint: str | None = None) -> list[TrendItem]:
    target = {'idol': 'idol_or_group', 'group': 'idol_or_group', 'actor': 'actor'}.get(
        category_hint, category_hint)
    if target in {'song', 'movie', 'drama'} and not all('_trend_candidate_types' in row for row in rows):
        rows = _work_entity_rows(rows, target)
    grouped = defaultdict(list)
    seen_articles = set()
    for row in rows:
        identity = row.get('url_hash') or row.get('article_id') or row.get('url')
        if identity in seen_articles:
            continue
        seen_articles.add(identity)
        if '_trend_entities' not in row and not _category_allowed(row, category_hint):
            continue
        names = row['_trend_entities'] if '_trend_entities' in row else extract_candidates(row.get('title') or '')
        for name in names:
            candidate_type = row.get('_trend_candidate_types', {}).get(name, target or 'entertainer')
            if target in {'song', 'movie', 'drama', 'actor', 'idol_or_group'} and candidate_type != target:
                continue
            grouped[name.casefold()].append((name, row))
    if not grouped:
        return []
    max_mentions = max(len(items) for items in grouped.values())
    max_sources = max(1, max(len({_source_key(item[1]) for item in items if _source_key(item[1])}) for items in grouped.values()))
    result = []
    for items in grouped.values():
        name = items[0][0]
        articles = [item[1] for item in items]
        sources = {_source_key(article) for article in articles if _source_key(article)}
        latest = max((value for article in articles if (value := _as_utc(article.get('published_at'))) is not None), default=None)
        age_days = max(0, (now - latest).total_seconds() / 86400) if latest else 999
        recency = 1 / (1 + age_days / max(config.TREND_DEFAULT_DAYS, 1))
        score = (len(articles) / max_mentions * config.TREND_MENTION_WEIGHT
                 + len(sources) / max_sources * config.TREND_SOURCE_WEIGHT
                 + recency * config.TREND_RECENCY_WEIGHT)
        representative = tuple(sorted(articles, key=lambda article: _as_utc(article.get('published_at')) or datetime.min.replace(tzinfo=timezone.utc), reverse=True)[:2])
        candidate_type = next(
            (article.get('_trend_candidate_types', {}).get(name, target or 'entertainer')
             for article in articles), target or 'entertainer')
        result.append(TrendItem(name, len(articles), len(sources), latest, score,
                                representative, candidate_type))
    minimum = config.TREND_MIN_ARTICLE_COUNT
    eligible = [item for item in result if item.mention_count >= minimum]
    if not eligible:
        eligible = [item for item in result if item.mention_count >= 1]
    diagnostics.record(ranking_candidates=len(eligible))
    diagnostics.record(final_candidates=[
        {'name': item.name, 'candidate_type': item.candidate_type,
         'context': item.candidate_type, 'mention_count': item.mention_count,
         'source_count': item.source_count}
        for item in sorted(eligible, key=lambda value: (-value.score, -value.mention_count))[:top_k]
    ])
    return sorted(eligible, key=lambda item: (-item.score, -item.mention_count, -(item.latest_at.timestamp() if item.latest_at else 0)))[:top_k]


def aggregate(time_range: str, top_k: int = config.TREND_TOP_K,
              category_hint: str | None = None, now: datetime | None = None) -> tuple[list[TrendItem], int]:
    now = now or datetime.now(timezone.utc)
    rows = db.fetch_all(TREND_ARTICLES_QUERY, (period_start(time_range, now), now, config.TREND_ARTICLE_LIMIT))
    diagnostics.record(articles_scanned=len(rows), raw_entities=0, eligible_entities=0,
                       period_start=period_start(time_range, now), period_end=now,
                       article_limit=config.TREND_ARTICLE_LIMIT)
    target = {'idol': 'idol_or_group', 'group': 'idol_or_group', 'actor': 'actor'}.get(
        category_hint, category_hint)
    extractor = {
        'song': 'extract_song_candidates', 'movie': 'extract_movie_candidates',
        'drama': 'extract_drama_candidates', 'actor': 'extract_person_candidates',
        'idol_or_group': 'extract_idol_group_candidates',
    }.get(target, 'extract_person_candidates')
    diagnostics.record(target_type=target or 'celebrity_general', extractor_used=extractor)
    # Work extractors never fall back to person extraction. Person/group
    # extraction remains cached by exact DB passages and filtered per query.
    selected = [row for row in rows if _category_allowed(row, None) or
                re.search(r'아이돌|걸그룹|보이그룹|배우|가수', _passage(row))]
    if target in {'song', 'movie', 'drama'}:
        grounded = _work_entity_rows(selected, target) if selected else []
    elif target == 'idol_or_group':
        grounded = extract_idol_group_candidates(selected) if selected else []
    else:
        grounded = extract_person_candidates(selected, category_hint) if selected else []
    return aggregate_rows(grounded, now, top_k, category_hint), len(rows)
