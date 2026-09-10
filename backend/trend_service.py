"""Database-backed trend ranking without changing the public API contract."""

import logging
import re
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from functools import lru_cache
from urllib.parse import urlsplit
from datetime import date, datetime, timedelta, timezone

if __package__:
    from . import config, db, diagnostics
    from .openai_client import OpenAIServiceError, extract_trend_entities
    from .query_utils import ENTITY_ALIASES
else:
    import config
    import db
    import diagnostics
    from openai_client import OpenAIServiceError, extract_trend_entities
    from query_utils import ENTITY_ALIASES

logger = logging.getLogger(__name__)

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
QUOTED_PATTERN = re.compile(r'["\'“‘「『]([^"\'”’」』]{2,20})["\'”’」』]')


@dataclass(frozen=True)
class TrendItem:
    name: str
    mention_count: int
    source_count: int
    latest_at: datetime | None
    score: float
    articles: tuple[dict, ...]


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


def _passage(row: dict) -> str:
    body = re.sub(r'\s+', ' ', row.get('content') or '')
    roles = [match.group() for match in re.finditer(
        r'[^.!?]{0,30}(?:배우|아이돌|그룹|가수|방송인)[^.!?]{0,120}', body)]
    return (row.get('title') or '') + '\n' + (' '.join(roles[:3]) + '\n' + body[:200])[:600]


@lru_cache(maxsize=32)
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


def _entity_rows(rows: list[dict], category_hint: str | None) -> list[dict]:
    passages = [_passage(row) for row in rows]
    batches = [tuple(passages[i:i + config.TREND_ENTITY_BATCH_SIZE])
               for i in range(0, len(passages), config.TREND_ENTITY_BATCH_SIZE)]
    mentions = [[] for _ in rows]
    failures = 0
    with ThreadPoolExecutor(max_workers=config.TREND_ENTITY_WORKERS) as pool:
        futures = [pool.submit(_extract_batch, batch) for batch in batches]
        offset = 0
        for batch, future in zip(batches, futures):
            try:
                for name, kind, index in future.result():
                    mentions[offset + index].append((name, kind))
            except OpenAIServiceError:
                failures += 1
                for index, passage in enumerate(batch):
                    mentions[offset + index].extend(_explicit_entities([passage]))
            offset += len(batch)
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
                       entity_extraction_failures=failures)
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
        result.append(TrendItem(name, len(articles), len(sources), latest, score, representative))
    minimum = config.TREND_MIN_ARTICLE_COUNT
    eligible = [item for item in result if item.mention_count >= minimum]
    if not eligible:
        eligible = [item for item in result if item.mention_count >= 1]
    diagnostics.record(ranking_candidates=len(eligible))
    return sorted(eligible, key=lambda item: (-item.score, -item.mention_count, -(item.latest_at.timestamp() if item.latest_at else 0)))[:top_k]


def aggregate(time_range: str, top_k: int = config.TREND_TOP_K,
              category_hint: str | None = None, now: datetime | None = None) -> tuple[list[TrendItem], int]:
    now = now or datetime.now(timezone.utc)
    rows = db.fetch_all(TREND_ARTICLES_QUERY, (period_start(time_range, now), now, config.TREND_ARTICLE_LIMIT))
    diagnostics.record(articles_scanned=len(rows), raw_entities=0, eligible_entities=0,
                       period_start=period_start(time_range, now), period_end=now,
                       article_limit=config.TREND_ARTICLE_LIMIT)
    # Use the same batches for different population questions; extraction is
    # cached by exact DB passages, while population filtering remains per query.
    selected = [row for row in rows if _category_allowed(row, None) or
                re.search(r'아이돌|걸그룹|보이그룹|배우|가수', _passage(row))]
    grounded = _entity_rows(selected, category_hint) if selected else []
    return aggregate_rows(grounded, now, top_k, category_hint), len(rows)
