"""
TMDB 영화·드라마·예능 수집기

단독 테스트:
    uv run python collector/sources_tmdb.py

단독 실행 시 DB에 저장하지 않고 수집 결과만 출력합니다.
"""

import os
from datetime import datetime
from pathlib import Path

import requests
from dotenv import load_dotenv


# 프로젝트 최상위 .env 로드
load_dotenv(Path(__file__).resolve().parents[1] / ".env")


TMDB_API_BASE_URL = "https://api.themoviedb.org/3"
TMDB_SITE_URL = "https://www.themoviedb.org"

SOURCE_NAME = "TMDB"

# TMDB TV 장르 ID
DRAMA_GENRE_ID = 18
REALITY_GENRE_ID = 10764
TALK_GENRE_ID = 10767


class TMDBAPIError(RuntimeError):
    """TMDB API 요청 또는 응답 처리 실패."""


def _get_access_token() -> str:
    """환경변수에서 TMDB 읽기 액세스 토큰을 가져옵니다."""
    token = os.getenv("TMDB_ACCESS_TOKEN", "").strip()

    if not token:
        raise TMDBAPIError(
            "TMDB_ACCESS_TOKEN 환경변수가 설정되지 않았습니다."
        )

    return token


def _request(endpoint: str, params: dict) -> dict:
    """TMDB API를 호출하고 JSON 응답을 반환합니다."""
    headers = {
        "Authorization": f"Bearer {_get_access_token()}",
        "accept": "application/json",
    }

    try:
        response = requests.get(
            f"{TMDB_API_BASE_URL}{endpoint}",
            headers=headers,
            params=params,
            timeout=20,
        )
        response.raise_for_status()
        data = response.json()
    except requests.RequestException as error:
        raise TMDBAPIError(
            f"TMDB API 호출에 실패했습니다: {endpoint}"
        ) from error
    except ValueError as error:
        raise TMDBAPIError(
            "TMDB API의 JSON 응답을 해석하지 못했습니다."
        ) from error

    if data.get("success") is False:
        raise TMDBAPIError(
            data.get("status_message", "TMDB API 오류가 발생했습니다.")
        )

    return data


def _clean(value: str | None, default: str = "") -> str:
    """문자열 앞뒤 공백을 제거합니다."""
    if not value:
        return default

    return value.strip()


def _published_at(value: str | None) -> str:
    """날짜가 없으면 현재 수집 시각을 반환합니다."""
    value = _clean(value)

    if value:
        return value

    return datetime.now().isoformat(timespec="seconds")


def _movie_content(movie: dict) -> str:
    """영화 정보를 임베딩용 자연어 본문으로 변환합니다."""
    title = _clean(movie.get("title"), "제목 미상")
    original_title = _clean(movie.get("original_title"), "미상")
    release_date = _clean(movie.get("release_date"), "미정")
    overview = _clean(movie.get("overview"), "줄거리 정보 없음")

    return "\n".join([
        f"영화명: {title}",
        f"원제: {original_title}",
        f"한국 개봉일: {release_date}",
        f"줄거리: {overview}",
    ])


def _tv_category(program: dict) -> str | None:
    """
    TMDB 장르 ID를 기준으로 드라마와 예능을 구분합니다.

    분류가 명확하지 않은 TV 프로그램은 저장하지 않습니다.
    """
    genre_ids = set(program.get("genre_ids") or [])

    if DRAMA_GENRE_ID in genre_ids:
        return "drama"

    if REALITY_GENRE_ID in genre_ids or TALK_GENRE_ID in genre_ids:
        return "show"

    return None


def _tv_content(program: dict, category: str) -> str:
    """TV 프로그램 정보를 임베딩용 자연어 본문으로 변환합니다."""
    title = _clean(program.get("name"), "제목 미상")
    original_name = _clean(program.get("original_name"), "미상")
    first_air_date = _clean(program.get("first_air_date"), "미정")
    overview = _clean(program.get("overview"), "프로그램 설명 없음")

    content_type = "드라마" if category == "drama" else "예능·방송"

    return "\n".join([
        f"프로그램명: {title}",
        f"원제: {original_name}",
        f"분류: {content_type}",
        f"첫 방영일: {first_air_date}",
        f"프로그램 설명: {overview}",
    ])


def fetch_upcoming_movies(max_pages: int = 3) -> list[dict]:
    """한국 개봉 예정 영화 정보를 수집합니다."""
    if max_pages < 1:
        raise ValueError("max_pages는 1 이상이어야 합니다.")

    articles = []
    seen_ids = set()

    for page in range(1, max_pages + 1):
        data = _request(
            "/movie/upcoming",
            {
                "language": "ko-KR",
                "region": "KR",
                "page": page,
            },
        )

        results = data.get("results") or []

        if not results:
            break

        for movie in results:
            movie_id = movie.get("id")
            title = _clean(movie.get("title"))

            if not movie_id or not title or movie_id in seen_ids:
                continue

            seen_ids.add(movie_id)

            articles.append({
                "title": f"{title} 개봉 정보",
                "url": f"{TMDB_SITE_URL}/movie/{movie_id}",
                "published_at": _published_at(
                    movie.get("release_date")
                ),
                "source_name": SOURCE_NAME,
                "category": "movie",
                "content": _movie_content(movie),
            })

        if page >= int(data.get("total_pages") or 1):
            break

    return articles


def fetch_korean_tv(max_pages: int = 3) -> list[dict]:
    """현재 방영 중인 한국 드라마·예능 정보를 수집합니다."""
    if max_pages < 1:
        raise ValueError("max_pages는 1 이상이어야 합니다.")

    articles = []
    seen_ids = set()

    for page in range(1, max_pages + 1):
        data = _request(
            "/tv/on_the_air",
            {
                "language": "ko-KR",
                "page": page,
            },
        )

        results = data.get("results") or []

        if not results:
            break

        for program in results:
            program_id = program.get("id")
            title = _clean(program.get("name"))
            origin_countries = program.get("origin_country") or []

            # 한국 제작 프로그램만 허용
            if (
                not program_id
                or not title
                or "KR" not in origin_countries
                or program_id in seen_ids
            ):
                continue

            category = _tv_category(program)

            # 장르상 드라마·예능으로 확인되지 않으면 제외
            if category is None:
                continue

            seen_ids.add(program_id)

            label = "드라마" if category == "drama" else "예능"

            articles.append({
                "title": f"{title} {label} 방영 정보",
                "url": f"{TMDB_SITE_URL}/tv/{program_id}",
                "published_at": _published_at(
                    program.get("first_air_date")
                ),
                "source_name": SOURCE_NAME,
                "category": category,
                "content": _tv_content(program, category),
            })

        if page >= int(data.get("total_pages") or 1):
            break

    return articles


def fetch_tmdb_articles(max_pages: int = 3) -> list[dict]:
    """
    영화·드라마·예능 데이터를 수집하여
    기존 RSS 기사와 같은 형태로 반환합니다.
    """
    articles = []

    articles.extend(fetch_upcoming_movies(max_pages=max_pages))
    articles.extend(fetch_korean_tv(max_pages=max_pages))

    return articles

# 터미널 출력용 (테스트) 추후, main 함수 전부 제거
def main():
    """DB 저장 없이 수집 결과를 출력합니다."""
    articles = fetch_tmdb_articles(max_pages=5)

    counts = {
        "movie": 0,
        "drama": 0,
        "show": 0,
    }

    for article in articles:
        counts[article["category"]] += 1

    print(f"TMDB 전체 수집 건수: {len(articles)}")
    print(f"영화: {counts['movie']}건")
    print(f"드라마: {counts['drama']}건")
    print(f"예능: {counts['show']}건")
    print()

    for index, article in enumerate(articles, start=1):
        print(
            f"[{index}] "
            f"[{article['category']}] "
            f"{article['title']}"
        )
        print(article["content"])
        print(f"URL: {article['url']}")
        print("-" * 60)


if __name__ == "__main__":
    main()