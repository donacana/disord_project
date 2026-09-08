"""
연예·문화 뉴스 수집기 (RSS 전용)
    python collector/collector.py

네이버 API 수집은 별도 프로그램(sources_naver.py)에서 독립적으로 실행됨.
    python collector/sources_naver.py

흐름: RSS 호출 → 본문 추출 → 무관 기사 필터 → 중복 체크 → DB 저장
"""

import os
import hashlib
from datetime import datetime
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode

import feedparser
import trafilatura
import psycopg
from dotenv import load_dotenv

from sources_rss import EXTRA_RSS_SOURCES

# ── 환경변수 로드 ──────────────────────────────────────────────
load_dotenv()
DATABASE_URL = os.environ["DATABASE_URL"]

# ── ① 수집 소스 목록 ────────────────────────────────────────────
# 연예 전용 RSS만 사용 (culture.xml처럼 범위 넓은 피드는 무관 기사가 섞여 제외)
RSS_SOURCES = [
    {"source_name": "연합뉴스", "url": "https://www.yna.co.kr/rss/entertainment.xml", "category": "celeb"},
    {"source_name": "한국경제", "url": "https://www.hankyung.com/feed/entertainment", "category": "celeb"},
    {"source_name": "스포츠경향", "url": "https://sports.khan.co.kr/rss/entertainment_tv", "category": "show"},
    {"source_name": "스포츠경향", "url": "https://sports.khan.co.kr/rss/entertainment_music", "category": "music"},
    {"source_name": "스포츠경향", "url": "https://sports.khan.co.kr/rss/entertainment_movie", "category": "movie"},
    {"source_name": "스포츠경향", "url": "https://sports.khan.co.kr/rss/entertainment", "category": "celeb"},
]
# 팀원 A가 sources_rss.py에 추가한 소스 병합 (기존 리스트 정의와 분리된 별도 줄)
RSS_SOURCES = RSS_SOURCES + EXTRA_RSS_SOURCES

# ── ② 카테고리 세분화 규칙 (제목 키워드 매칭) ─────────────────────
CATEGORY_RULES = [
    ("music", ["신곡", "컴백", "앨범", "음원", "차트", "발매", "싱글"]),
    ("drama", ["드라마", "회차", "시청률", "종영", "첫방", "본방",
               "tvN", "JTBC", "넷플릭스", "티빙", "디즈니", "웨이브", "쿠팡플레이"]),
    ("show", ["예능", "방송", "출연진", "MC", "라디오"]),
    ("event", ["콘서트", "팬미팅", "시상식", "투어", "티켓", "무대"]),
    ("movie", ["개봉", "박스오피스", "관객", "영화제", "감독", "천만"]),
    ("webtoon", ["웹툰", "웹소설", "드라마화", "영상화"]),
]

# ── ③ 명백히 무관한 기사 걸러내기 (부고/행정/사회 뉴스 등) ─────────
IRRELEVANT_KEYWORDS = [
    "부고", "공정위", "선관위", "기후부", "수해", "단속", "지원금",
    "국정감사", "성명", "규탄", "집회", "판결", "검찰", "구속",
    "[포토]",
]


def is_relevant(title: str) -> bool:
    return not any(k in title for k in IRRELEVANT_KEYWORDS)


def guess_category(title: str, default: str) -> str:
    for cat, keywords in CATEGORY_RULES:
        if any(k in title for k in keywords):
            return cat
    return default


def normalize_url(url: str) -> str:
    parsed = urlparse(url)
    clean_qs = [(k, v) for k, v in parse_qsl(parsed.query) if not k.startswith("utm_")]
    return urlunparse(parsed._replace(query=urlencode(clean_qs)))


def url_hash(url: str) -> str:
    return hashlib.sha256(normalize_url(url).encode()).hexdigest()


def get_or_create_source_id(conn, name: str) -> int:
    """소스 이름으로 sources.id를 찾고, 없으면 새로 만듦"""
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM sources WHERE name = %s", (name,))
        row = cur.fetchone()
        if row:
            return row[0]
        cur.execute("INSERT INTO sources (name) VALUES (%s) RETURNING id", (name,))
        return cur.fetchone()[0]


def fetch_articles():
    """④ RSS 실제 호출 + 무관 기사 필터링"""
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    articles = []
    for source in RSS_SOURCES:
        feed = feedparser.parse(source["url"], request_headers=headers)
        for entry in feed.entries[:50]:
            if not is_relevant(entry.title):
                continue
            articles.append({
                "title": entry.title,
                "url": entry.link,
                "published_at": entry.get("published") or datetime.now().isoformat(),
                "source_name": source["source_name"],
                "category": guess_category(entry.title, source["category"]),
            })
    return articles


def extract_content(url: str):
    """⑤ 기사 본문 추출"""
    downloaded = trafilatura.fetch_url(url)
    if not downloaded:
        return None
    return trafilatura.extract(downloaded)


def save_article(conn, article: dict, content: str, source_id: int):
    """⑥ DB 저장 — url_hash UNIQUE라 중복이면 조용히 무시
    임베딩은 여기서 만들지 않음 — backend/embed_articles.py가 배치로 처리함
    """
    h = url_hash(article["url"])
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO articles
                (domain, category, title, content, url, url_hash,
                 source_id, source_name, published_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (url_hash) DO NOTHING
            RETURNING id
            """,
            (
                "culture", article["category"], article["title"], content,
                article["url"], h, source_id, article["source_name"], article["published_at"],
            ),
        )
        row = cur.fetchone()
        if row is None:
            return None
        return row[0]


def main():
    conn = psycopg.connect(DATABASE_URL)
    fetched, inserted, duplicates, skipped = 0, 0, 0, 0

    source_ids = {}
    with conn:
        for source in RSS_SOURCES:
            source_ids[source["source_name"]] = get_or_create_source_id(conn, source["source_name"])

        articles = fetch_articles()
        print(f"RSS에서 {len(articles)}건 목록 확보 (무관 기사 필터링 후), 본문 추출 시작...")

        for i, article in enumerate(articles, 1):
            fetched += 1
            content = extract_content(article["url"])
            if not content or len(content) < 200:
                skipped += 1
                print(f"[{i}/{len(articles)}] 본문 부족, 건너뜀: {article['title'][:30]}")
                continue

            source_id = source_ids[article["source_name"]]
            article_id = save_article(conn, article, content, source_id)
            if article_id:
                inserted += 1
                print(f"[{i}/{len(articles)}] 저장: {article['title'][:30]}")
            else:
                duplicates += 1

        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO collection_runs
                    (fetched_count, inserted_count, duplicate_count, status, finished_at)
                VALUES (%s, %s, %s, 'success', now())
                """,
                (fetched, inserted, duplicates),
            )

    print(f"\n수집 완료: 조회 {fetched}건 / 신규 {inserted}건 / 중복 {duplicates}건 / 본문부족 {skipped}건")
    conn.close()


if __name__ == "__main__":
    main()