"""Database-backed trend ranking without changing the public API contract."""

import logging
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

if __package__:
    from . import config, db
else:
    import config
    import db

logger = logging.getLogger(__name__)

TREND_ARTICLES_QUERY = """
SELECT id AS article_id, title, content, category, source_name, url,
       url_hash, published_at, collected_at
FROM public.articles
WHERE published_at >= %s
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
        if not _category_allowed(row, category_hint):
            continue
        for name in extract_candidates(row.get('title') or ''):
            grouped[name.casefold()].append((name, row))
    if not grouped:
        return []
    max_mentions = max(len(items) for items in grouped.values())
    max_sources = max(len({item[1].get('source_name') for item in items}) for items in grouped.values())
    result = []
    for items in grouped.values():
        name = items[0][0]
        articles = [item[1] for item in items]
        sources = {article.get('source_name') for article in articles if article.get('source_name')}
        latest = max((_as_utc(article.get('published_at')) for article in articles), default=None)
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
    return sorted(eligible, key=lambda item: (-item.score, -item.mention_count, -(item.latest_at.timestamp() if item.latest_at else 0)))[:top_k]


def aggregate(time_range: str, top_k: int = config.TREND_TOP_K,
              category_hint: str | None = None, now: datetime | None = None) -> tuple[list[TrendItem], int]:
    now = now or datetime.now(timezone.utc)
    rows = db.fetch_all(TREND_ARTICLES_QUERY, (period_start(time_range, now), config.TREND_ARTICLE_LIMIT))
    return aggregate_rows(rows, now, top_k, category_hint), len(rows)