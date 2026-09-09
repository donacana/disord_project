"""
NAVER API HUB 뉴스 대량 수집

기능
- NAVER 뉴스 검색
- 2026년 기사만 수집
- 검색어별 최대 1000개 결과 탐색
- 기사 본문 추출
- URL 중복 제거
- 본문 완전 중복 제거
- Neon PostgreSQL articles 저장
- 임베딩은 수행하지 않음
"""

import os
import re
import hashlib
from html import unescape
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import (
    urlparse,
    urlunparse,
    parse_qsl,
    urlencode,
)

import psycopg
import requests
import trafilatura
from dotenv import load_dotenv


# =========================================================
# 설정
# =========================================================

load_dotenv(
    Path(__file__).with_name(".env"),
    override=False,
)

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
NAVER_CLIENT_ID = os.getenv("NAVER_CLIENT_ID", "").strip()
NAVER_CLIENT_SECRET = os.getenv("NAVER_CLIENT_SECRET", "").strip()

NAVER_API_URL = (
    "https://naverapihub.apigw.ntruss.com/search/v1/news"
)

SOURCE_NAME = "네이버뉴스"
DOMAIN = "culture"

# 2026년 전체 데이터 대상
START_DATE = datetime(
    2026,
    1,
    1,
    tzinfo=timezone.utc,
)

# NAVER 검색 한 페이지 최대 결과 수
DISPLAY = 100

# start = 1, 101, ..., 901
MAX_START = 901

END_DATE = datetime(2027, 1, 1, tzinfo=timezone.utc)
REQUEST_TIMEOUT = 20

# 본문 최소 길이
MIN_CONTENT_LENGTH = 200


# =========================================================
# 검색어
# =========================================================

NAVER_SEARCH_QUERIES = [
    # -----------------------------------------------------
    # 음악 / 아이돌
    # -----------------------------------------------------
    {"query": "아이돌", "category": "music"},
    {"query": "KPOP", "category": "music"},
    {"query": "아이돌 컴백", "category": "music"},
    {"query": "아이돌 신곡", "category": "music"},
    {"query": "아이돌 앨범", "category": "music"},
    {"query": "걸그룹", "category": "music"},
    {"query": "보이그룹", "category": "music"},
    {"query": "KPOP 컴백", "category": "music"},

    # -----------------------------------------------------
    # 공연 / 콘서트
    # -----------------------------------------------------
    {"query": "아이돌 공연", "category": "event"},
    {"query": "아이돌 콘서트", "category": "event"},
    {"query": "아이돌 팬미팅", "category": "event"},
    {"query": "KPOP 공연", "category": "event"},
    {"query": "콘서트", "category": "event"},
    {"query": "공연", "category": "event"},
    {"query": "팬미팅", "category": "event"},

    # -----------------------------------------------------
    # 연예인 활동
    # -----------------------------------------------------
    {"query": "연예인 활동", "category": "celeb"},
    {"query": "연예인 광고", "category": "celeb"},
    {"query": "연예인 화보", "category": "celeb"},
    {"query": "연예인 행사", "category": "celeb"},
    {"query": "아이돌 광고", "category": "celeb"},
    {"query": "아이돌 화보", "category": "celeb"},

    # -----------------------------------------------------
    # 드라마 / 배우
    # -----------------------------------------------------
    {"query": "드라마", "category": "drama"},
    {"query": "드라마 출연", "category": "drama"},
    {"query": "드라마 캐스팅", "category": "drama"},
    {"query": "배우 출연", "category": "celeb"},
    {"query": "배우 광고", "category": "celeb"},
    {"query": "배우 화보", "category": "celeb"},

    # -----------------------------------------------------
    # 예능 / 방송
    # -----------------------------------------------------
    {"query": "예능", "category": "show"},
    {"query": "예능 출연", "category": "show"},
    {"query": "방송 출연", "category": "show"},

    # -----------------------------------------------------
    # 영화
    # -----------------------------------------------------
    {"query": "영화", "category": "movie"},
    {"query": "영화 개봉", "category": "movie"},
    {"query": "신작 영화", "category": "movie"},
    {"query": "한국 영화", "category": "movie"},
    {"query": "박스오피스", "category": "movie"},

    # -----------------------------------------------------
    # 웹툰
    # -----------------------------------------------------
    {"query": "웹툰", "category": "webtoon"},
    {"query": "웹툰 신작", "category": "webtoon"},
    {"query": "웹툰 드라마", "category": "webtoon"},

    # -----------------------------------------------------
    # 문화 / 트렌드
    # -----------------------------------------------------
    {"query": "유행 트렌드", "category": "trend"},
    {"query": "팝업스토어", "category": "trend"},
    {"query": "캐릭터 굿즈", "category": "trend"},
    {"query": "키링 유행", "category": "trend"},
    {"query": "캐릭터 팝업", "category": "trend"},
    {"query": "굿즈 팝업", "category": "trend"},
]


# =========================================================
# 환경변수 검사
# =========================================================

def validate_environment() -> None:
    required = {
        "DATABASE_URL": DATABASE_URL,
        "NAVER_CLIENT_ID": NAVER_CLIENT_ID,
        "NAVER_CLIENT_SECRET": NAVER_CLIENT_SECRET,
    }

    missing = [
        name
        for name, value in required.items()
        if not value
    ]

    if missing:
        raise RuntimeError(
            "필수 환경변수가 없습니다: "
            + ", ".join(missing)
        )


# =========================================================
# 인증 헤더
# =========================================================

def get_headers() -> dict:
    return {
        "X-NCP-APIGW-API-KEY-ID": NAVER_CLIENT_ID,
        "X-NCP-APIGW-API-KEY": NAVER_CLIENT_SECRET,
    }


# =========================================================
# URL 정규화
# =========================================================

def normalize_url(url: str) -> str:
    parsed = urlparse(url)

    clean_query = [
        (key, value)
        for key, value in parse_qsl(
            parsed.query,
            keep_blank_values=True,
        )
        if not key.lower().startswith("utm_")
    ]

    return urlunparse(
        parsed._replace(
            query=urlencode(clean_query),
            fragment="",
        )
    )


# =========================================================
# 텍스트 정규화
# =========================================================

def normalize_text(text: str) -> str:
    text = unescape(text or "")

    # HTML 태그 제거
    text = re.sub(
        r"<[^>]+>",
        " ",
        text,
    )

    # 여러 공백/줄바꿈을 하나로
    text = re.sub(
        r"\s+",
        " ",
        text,
    )

    return text.strip()


# =========================================================
# SHA256
# =========================================================

def make_hash(text: str) -> str:
    return hashlib.sha256(
        text.encode("utf-8")
    ).hexdigest()


# =========================================================
# 날짜 처리
# =========================================================

def parse_pub_date(value: str | None) -> datetime | None:
    if not value:
        return None

    try:
        parsed = parsedate_to_datetime(value)

        if parsed.tzinfo is None:
            parsed = parsed.replace(
                tzinfo=timezone.utc
            )

        return parsed.astimezone(
            timezone.utc
        )

    except Exception:
        return None


def is_2026_article(
    published_at: datetime | None,
) -> bool:

    if published_at is None:
        return False

    return START_DATE <= published_at < END_DATE and published_at <= datetime.now(timezone.utc)


# =========================================================
# 기사 본문 추출
# =========================================================

def extract_content(
    url: str,
) -> str | None:

    try:
        html = trafilatura.fetch_url(
            url
        )

        if not html:
            return None

        content = trafilatura.extract(
            html,
            include_comments=False,
            include_tables=False,
        )

        if not content:
            return None

        content = content.strip()

        if len(content) < MIN_CONTENT_LENGTH:
            return None

        return content

    except Exception as error:
        print(
            f"[본문 추출 실패] {url}"
        )
        print(error)

        return None


# =========================================================
# source_id
# =========================================================

def get_source_id(
    conn,
) -> int:

    with conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT id
            FROM public.sources
            WHERE name = %s
            LIMIT 1
            """,
            (SOURCE_NAME,),
        )

        row = cursor.fetchone()

    if not row:
        raise RuntimeError(
            "public.sources 테이블에 "
            "'네이버뉴스' source가 없습니다."
        )

    # dict_row / tuple 모두 대응
    if isinstance(row, dict):
        return int(row["id"])

    return int(row[0])


# =========================================================
# DB 중복 검사
# =========================================================

def already_exists(
    conn,
    url_hash: str,
    content_hash: str,
) -> bool:

    with conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT 1
            FROM public.articles
            WHERE url_hash = %s
               OR content_hash = %s
            LIMIT 1
            """,
            (
                url_hash,
                content_hash,
            ),
        )

        return cursor.fetchone() is not None


# =========================================================
# 기사별 트랜잭션: 실패해도 앞서 저장한 기사는 유지
# =========================================================


def save_article(conn, article: dict, source_id: int) -> bool:
    with conn.transaction():
        with conn.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO public.articles (
                    source_id, category, domain, title, content, url,
                    url_hash, content_hash, source_name, published_at,
                    collected_at, is_embedded
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW(), FALSE)
                ON CONFLICT (url_hash) DO NOTHING
                RETURNING id
                """,
                (source_id, article['category'], DOMAIN, article['title'],
                 article['content'], article['url'], article['url_hash'],
                 article['content_hash'], article['source_name'], article['published_at']),
            )
            row = cursor.fetchone()
    # Transaction commit has succeeded before the saved counter is incremented.
    return row is not None


def fetch_page(query: str, start: int, display: int = DISPLAY) -> list[dict]:
    if not 1 <= display <= 100 or not 1 <= start <= 1000 or start + display - 1 > 1000:
        raise ValueError('검색 범위는 1~1000, display는 1~100이어야 합니다.')
    response = requests.get(
        NAVER_API_URL, headers=get_headers(),
        params={'query': query, 'display': display, 'start': start, 'sort': 'date'},
        timeout=REQUEST_TIMEOUT,
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict) or not isinstance(payload.get('items'), list):
        raise ValueError('NAVER 응답 items 형식 오류')
    items = payload['items']
    if len(items) > display or any(not isinstance(item, dict) for item in items):
        raise ValueError('NAVER 응답 기사 형식 오류')
    return items


def empty_counts() -> dict:
    return dict(api_calls=0, pages=0, searched=0, year_filtered=0, saved=0,
                duplicate=0, content_failed=0, date_invalid=0, out_of_range=0,
                invalid_article=0, errors=0)


def collect_query(conn, source_id: int, query: str, category: str, *,
                  display: int = DISPLAY, seen_url_hashes=None,
                  seen_content_hashes=None, now: datetime | None = None) -> dict:
    if not conn.autocommit:
        raise ValueError('기사별 저장 확정을 위해 autocommit=True 연결이 필요합니다.')
    if not 1 <= display <= 100:
        raise ValueError('display는 1~100이어야 합니다.')
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError('now에는 timezone이 필요합니다.')
    seen_url_hashes = seen_url_hashes if seen_url_hashes is not None else set()
    seen_content_hashes = seen_content_hashes if seen_content_hashes is not None else set()
    result = empty_counts()
    result.update(last_start=None, oldest_published_at=None, newest_published_at=None,
                  stop_reason='result_limit', fatal_api_error=False)
    print(f'[NAVER 검색] {query} / {category}')
    for start in range(1, MAX_START + DISPLAY, display):
        page_display = min(display, 1001 - start)
        result['last_start'] = start
        result['api_calls'] += 1  # Count failed attempts too.
        print(f'[페이지] query={query} start={start} display={page_display}')
        try:
            items = fetch_page(query, start, page_display)
        except (requests.RequestException, ValueError) as error:
            result['errors'] += 1
            response = getattr(error, 'response', None)
            status = getattr(response, 'status_code', None)
            result['fatal_api_error'] = status in (401, 403, 429)
            result['stop_reason'] = 'api_error'
            print(f'[NAVER API 실패] query={query} start={start} status={status} type={type(error).__name__}')
            break
        result['pages'] += 1
        if not items:
            result['stop_reason'] = 'empty_page'
            break
        result['searched'] += len(items)
        page_dates = [parse_pub_date(item.get('pubDate')) for item in items]
        for item, published_at in zip(items, page_dates):
            if published_at is None:
                result['date_invalid'] += 1
                continue
            stamp = published_at.isoformat()
            result['oldest_published_at'] = min(result['oldest_published_at'] or stamp, stamp)
            result['newest_published_at'] = max(result['newest_published_at'] or stamp, stamp)
            if not (START_DATE <= published_at < END_DATE and published_at <= now):
                result['out_of_range'] += 1
                continue
            result['year_filtered'] += 1
            title = normalize_text(item.get('title') or '')
            url = item.get('originallink') or item.get('link')
            if not title or not isinstance(url, str) or urlparse(url).scheme not in ('http', 'https'):
                result['invalid_article'] += 1
                continue
            url = normalize_url(url)
            url_hash = make_hash(url)
            if url_hash in seen_url_hashes:
                result['duplicate'] += 1
                continue
            try:
                # Skip known URLs before downloading the full body.
                with conn.cursor() as cursor:
                    cursor.execute('SELECT 1 FROM public.articles WHERE url_hash = %s LIMIT 1', (url_hash,))
                    exists = cursor.fetchone() is not None
                if exists:
                    seen_url_hashes.add(url_hash)
                    result['duplicate'] += 1
                    continue
                content = extract_content(url)
                if not content:
                    result['content_failed'] += 1
                    continue
                content_hash = make_hash(normalize_text(content))
                if content_hash in seen_content_hashes or already_exists(conn, url_hash, content_hash):
                    seen_url_hashes.add(url_hash)
                    result['duplicate'] += 1
                    continue
                article = dict(title=title, content=content, url=url, url_hash=url_hash,
                               content_hash=content_hash, published_at=published_at,
                               source_name=SOURCE_NAME, category=category)
                saved = save_article(conn, article, source_id)
                seen_url_hashes.add(url_hash)
                if saved:
                    seen_content_hashes.add(content_hash)
                    result['saved'] += 1
                    print(f'[저장] {title}')
                else:
                    result['duplicate'] += 1
            except psycopg.Error as error:
                # Save transaction rolls back only this article; read statements
                # run in autocommit. Failed hashes remain retryable.
                result['errors'] += 1
                print(f'[DB 처리 실패] {title} type={type(error).__name__}')
        # Unknown dates cannot prove that a page is entirely older than 2026.
        if all(value is not None and value < START_DATE for value in page_dates):
            result['stop_reason'] = 'older_than_start'
            break
        if len(items) < page_display:
            result['stop_reason'] = 'last_page'
            break
    print(f'[검색어 완료] {query}: {result}')
    return result


def fetch_naver_news(query: str, category: str, display: int = DISPLAY) -> dict:
    """Keep the existing entry point for callers collecting one query."""
    validate_environment()
    with psycopg.connect(DATABASE_URL, connect_timeout=10, autocommit=True) as conn:
        return collect_query(conn, get_source_id(conn), query, category, display=display)


validate_config = validate_environment


def main() -> int:
    totals = empty_counts()
    seen_urls, seen_contents = set(), set()
    now = datetime.now(timezone.utc)
    print(f'NAVER 2026 수집: 검색어 {len(NAVER_SEARCH_QUERIES)}개, UTC {START_DATE.isoformat()}부터 현재까지')
    try:
        validate_environment()
        with psycopg.connect(DATABASE_URL, connect_timeout=10, autocommit=True) as conn:
            source_id = get_source_id(conn)
            for item in NAVER_SEARCH_QUERIES:
                result = collect_query(conn, source_id, item['query'], item['category'],
                                       seen_url_hashes=seen_urls, seen_content_hashes=seen_contents, now=now)
                for key in totals:
                    totals[key] += result[key]
                if result['fatal_api_error']:
                    print('[전체 중단] 인증/권한/호출 제한 오류')
                    break
    except (RuntimeError, psycopg.Error) as error:
        print(f'[수집 실패] type={type(error).__name__}')
        if isinstance(error, RuntimeError):
            print(error)  # Only our configuration/source messages, no secrets.
        return 1
    print(f'[전체 완료] {totals}')
    return 1 if totals['errors'] else 0


if __name__ == '__main__':
    raise SystemExit(main())
