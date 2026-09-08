"""
NAVER API HUB 뉴스 검색
- NAVER 뉴스 검색
- 기사 본문 추출
- URL 중복 제거
- 본문 완전 중복 제거
- Neon PostgreSQL articles 저장
"""

import os
import re
import hashlib
from html import unescape
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode

import requests
import trafilatura
import psycopg
from dotenv import load_dotenv


# =========================================================
# 환경변수
# =========================================================

load_dotenv(Path(__file__).resolve().parent / ".env")

DATABASE_URL = os.environ["DATABASE_URL"]
NAVER_CLIENT_ID = os.environ["NAVER_CLIENT_ID"]
NAVER_CLIENT_SECRET = os.environ["NAVER_CLIENT_SECRET"]


NAVER_API_URL = "https://naverapihub.apigw.ntruss.com/search/v1/news"


# =========================================================
# NAVER API 인증 헤더
# =========================================================

HEADERS = {
    "X-NCP-APIGW-API-KEY-ID": NAVER_CLIENT_ID,
    "X-NCP-APIGW-API-KEY": NAVER_CLIENT_SECRET,
}


def validate_config() -> None:
    missing = [
        name
        for name, value in (
            ("DATABASE_URL", DATABASE_URL),
            ("NAVER_CLIENT_ID", NAVER_CLIENT_ID),
            ("NAVER_CLIENT_SECRET", NAVER_CLIENT_SECRET),
        )
        if not value
    ]

    if missing:
        raise RuntimeError(
            "필수 환경변수가 없습니다: " + ", ".join(missing)
        )


# =========================================================
# URL 정규화
# =========================================================

def normalize_url(url: str) -> str:
    """
    같은 기사인데 추적 파라미터만 다른 경우를 동일 URL로 처리한다.
    """

    parsed = urlparse(url)

    clean_query = [
        (key, value)
        for key, value in parse_qsl(parsed.query)
        if not key.lower().startswith("utm_")
    ]

    return urlunparse(
        parsed._replace(
            query=urlencode(clean_query),
            fragment=""
        )
    )


# =========================================================
# 텍스트 정규화
# =========================================================

def normalize_text(text: str) -> str:
    """
    HTML 태그 / 공백 / 줄바꿈 차이만 제거한다.

    기사 내용 자체가 다르면 다른 데이터로 취급한다.
    """

    text = unescape(text)

    # HTML 태그 제거
    text = re.sub(r"<[^>]+>", " ", text)

    # 여러 공백/줄바꿈을 하나의 공백으로
    text = re.sub(r"\s+", " ", text)

    return text.strip()


# =========================================================
# SHA256 해시
# =========================================================

def make_hash(text: str) -> str:
    return hashlib.sha256(
        text.encode("utf-8")
    ).hexdigest()


# =========================================================
# 기사 본문 추출
# =========================================================

def extract_content(url: str) -> str | None:
    try:
        html = trafilatura.fetch_url(url)

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

        # 너무 짧으면 정상적인 기사 본문이 아니라고 판단
        if len(content) < 200:
            return None

        return content

    except Exception as e:
        print(f"[본문 추출 실패] {url}")
        print(e)

        return None


# =========================================================
# DB 중복 검사
# =========================================================

def already_exists(
    conn,
    url_hash: str,
    content_hash: str,
) -> bool:

    with conn.cursor() as cur:
        cur.execute(
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

        return cur.fetchone() is not None


def get_source_id(conn) -> int:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id
            FROM public.sources
            WHERE name = %s
            LIMIT 1
            """,
            ("네이버뉴스",),
        )
        row = cur.fetchone()
        if row is None:
            raise RuntimeError("public.sources에 name='네이버뉴스'가 없습니다.")
        return row[0]


# =========================================================
# DB 저장
# =========================================================

def save_article(
    conn,
    article: dict,
    source_id: int,
) -> bool:

    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO public.articles (
                    source_id,
                    category,
                    domain,
                    title,
                    content,
                    url,
                    url_hash,
                    content_hash,
                    source_name,
                    published_at,
                    collected_at,
                    is_embedded
                )
                VALUES (
                    %s,
                    %s,
                    %s,
                    %s,
                    %s,
                    %s,
                    %s,
                    %s,
                    %s,
                    %s,
                    NOW(),
                    FALSE
                )
                ON CONFLICT (url_hash)
                DO NOTHING
                RETURNING id
                """,
                (
                    source_id,
                    article["category"],
                    "culture",
                    article["title"],
                    article["content"],
                    article["url"],
                    article["url_hash"],
                    article["content_hash"],
                    article["source_name"],
                    article["published_at"],
                ),
            )

            row = cur.fetchone()

            if row is None:
                return False

            return True

    except Exception as e:
        print("[DB 저장 실패]")
        print(e)

        conn.rollback()

        return False


# =========================================================
# NAVER 뉴스 수집
# =========================================================

def fetch_naver_news(
    query: str,
    category: str,
    display: int = 100,
) -> dict:

    print()
    print("=" * 60)
    print(f"[NAVER 검색] {query}")
    print("=" * 60)

    try:
        response = requests.get(
            NAVER_API_URL,
            headers=HEADERS,
            params={
                "query": query,
                "display": display,
                "sort": "date",
            },
            timeout=20,
        )
        response.raise_for_status()
        items = response.json().get("items", [])
    except (requests.RequestException, ValueError) as error:
        print(f"[검색 실패] {query}: {error}")
        return {
            "searched": 0,
            "saved": 0,
            "duplicate": 0,
            "content_failed": 0,
        }

    # 이번 실행 중 중복 검사
    seen_url_hashes = set()
    seen_content_hashes = set()

    result = {
        "searched": len(items),
        "saved": 0,
        "duplicate": 0,
        "content_failed": 0,
    }

    with psycopg.connect(DATABASE_URL) as conn:
        source_id = get_source_id(conn)

        for item in items:

            # -------------------------------------------------
            # 제목
            # -------------------------------------------------

            title = normalize_text(
                item.get("title", "")
            )

            # -------------------------------------------------
            # URL
            # -------------------------------------------------

            url = (
                item.get("originallink")
                or item.get("link")
            )

            if not url:
                continue

            url = normalize_url(url)

            url_hash = make_hash(url)

            # -------------------------------------------------
            # 1차 중복
            # 이번 검색 결과 안에서 같은 URL
            # -------------------------------------------------

            if url_hash in seen_url_hashes:

                result["duplicate"] += 1

                print(
                    f"[중복 URL] {title}"
                )

                continue

            seen_url_hashes.add(url_hash)

            # -------------------------------------------------
            # 기사 본문 추출
            # -------------------------------------------------

            content = extract_content(url)

            if not content:

                result["content_failed"] += 1

                print(
                    f"[본문 없음] {title}"
                )

                continue

            # -------------------------------------------------
            # 본문 정규화
            # -------------------------------------------------

            normalized_content = normalize_text(
                content
            )

            content_hash = make_hash(
                normalized_content
            )

            # -------------------------------------------------
            # 2차 중복
            # 이번 검색 결과 안에서 본문 완전 동일
            # -------------------------------------------------

            if content_hash in seen_content_hashes:

                result["duplicate"] += 1

                print(
                    f"[중복 본문] {title}"
                )

                continue

            seen_content_hashes.add(
                content_hash
            )

            # -------------------------------------------------
            # 3차
            # 기존 DB에 URL 또는 본문이 이미 있는지 검사
            # -------------------------------------------------

            try:
                exists = already_exists(
                    conn,
                    url_hash,
                    content_hash,
                )
            except Exception as error:
                print(f"[DB 중복 확인 실패] {title}: {error}")
                conn.rollback()
                continue

            if exists:

                result["duplicate"] += 1

                print(
                    f"[DB 중복] {title}"
                )

                continue

            # -------------------------------------------------
            # DB 저장
            # -------------------------------------------------

            article = {
                "title": title,
                "content": content,
                "url": url,
                "url_hash": url_hash,
                "content_hash": content_hash,
                "published_at": item.get("pubDate")
                or datetime.now().isoformat(),
                "source_name": "네이버뉴스",
                "category": category,
            }

            if save_article(
                conn,
                article,
                source_id,
            ):
                result["saved"] += 1

                print(
                    f"[저장] {title}"
                )

        conn.commit()

    return result


# =========================================================
# 검색어
# =========================================================

NAVER_SEARCH_QUERIES = [

    # 음악
    {
        "query": "아이돌",
        "category": "music",
    },
    {
        "query": "KPOP",
        "category": "music",
    },

    # 드라마
    {
        "query": "드라마",
        "category": "drama",
    },

    # 방송 / 예능
    {
        "query": "예능",
        "category": "show",
    },

    # 영화
    {
        "query": "영화",
        "category": "movie",
    },

    # 웹툰
    {
        "query": "웹툰",
        "category": "webtoon",
    },

    # 행사 / 공연
    {
        "query": "공연",
        "category": "event",
    },

    # 트렌드
    {
        "query": "유행 트렌드",
        "category": "trend",
    },
    {
        "query": "팝업스토어",
        "category": "trend",
    },
    {
        "query": "캐릭터 굿즈",
        "category": "trend",
    },
]


# =========================================================
# 실행
# =========================================================

if __name__ == "__main__":

    print("=" * 60)
    print("NAVER 뉴스 수집 시작")
    print("=" * 60)

    try:
        validate_config()
    except RuntimeError as error:
        print(f"[환경변수 오류] {error}")
        raise SystemExit(1)

    total_searched = 0
    total_saved = 0
    total_duplicate = 0
    total_failed = 0

    for q in NAVER_SEARCH_QUERIES:

        try:
            result = fetch_naver_news(
                query=q["query"],
                category=q["category"],
            )

            total_saved += result["saved"]
            total_searched += result["searched"]
            total_duplicate += result["duplicate"]
            total_failed += result["content_failed"]

            print()
            print(
                f"[{q['query']}] "
                f"검색 {result['searched']}건 / "
                f"저장 {result['saved']}건 / "
                f"중복 {result['duplicate']}건 / "
                f"본문 실패 {result['content_failed']}건"
            )

        except Exception as e:

            print(
                f"[검색 실패] {q['query']}"
            )

            print(e)

    print()
    print("=" * 60)
    print("NAVER 뉴스 수집 완료")
    print(f"검색 건수: {total_searched}건")
    print(f"신규 저장: {total_saved}건")
    print(f"중복 제외: {total_duplicate}건")
    print(f"본문 실패: {total_failed}건")
    print("=" * 60)