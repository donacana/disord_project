"""
한국문화정보원 문화행사 공공 API 수집기

단독 테스트:
    uv run python collector/sources_public_event.py

단독 실행 시 DB에 저장하지 않고 수집 결과만 출력합니다.
"""

import os
from datetime import date, datetime, timedelta
from html import unescape
from urllib.parse import quote, unquote
from xml.etree import ElementTree

import requests
from dotenv import load_dotenv


load_dotenv()

PUBLIC_EVENT_API_URL = (
    "https://apis.data.go.kr/B553457/cultureinfo/period2"
)

SOURCE_NAME = "문화포털"
DEFAULT_CATEGORY = "event"

# 공연·연예문화 프로젝트에서 사용할 분야
ALLOWED_REALMS = {
    "연극",
    "음악",
    "콘서트",
    "국악",
    "무용",
    "발레",
    "뮤지컬",
    "오페라",
    "아동",
    "가족",
    "행사",
    "축제",
}


class PublicEventAPIError(RuntimeError):
    """문화행사 공공 API 요청 또는 응답 처리 실패."""


def _clean(value: str | None, default: str = "") -> str:
    """XML 문자열의 공백과 HTML 문자를 정리합니다."""
    if not value:
        return default

    return unescape(value).strip()


def _official_url(sequence: str, title: str) -> str:
    """
    API 인증키가 포함되지 않은 문화포털 검색 주소를 생성합니다.

    eventSeq를 함께 넣어 행사마다 URL이 겹치지 않도록 합니다.
    """
    encoded_title = quote(title)

    return (
        "https://www.culture.go.kr/search/search.do"
        f"?query={encoded_title}&eventSeq={sequence}"
    )


def _build_content(event: dict) -> str:
    """공연 정보를 RAG 임베딩에 사용할 자연어 본문으로 변환합니다."""
    location_parts = []

    if event["area"] and event["area"] != "미정":
        location_parts.append(event["area"])

    if event["sigungu"]:
        location_parts.append(event["sigungu"])

    location = " ".join(location_parts) or "미정"

    return "\n".join([
        f"행사명: {event['title']}",
        f"행사 분야: {event['realm']}",
        f"행사 기간: {event['start_date']}부터 {event['end_date']}까지",
        f"행사 장소: {event['place']}",
        f"행사 지역: {location}",
        f"제공 서비스: {event['service_name']}",
    ])


def _is_allowed_realm(realm: str) -> bool:
    """공연·행사 분야에 해당하는 데이터만 허용합니다."""
    return any(keyword in realm for keyword in ALLOWED_REALMS)


def _request_page(
    api_key: str,
    start_date: date,
    end_date: date,
    page: int,
    rows: int,
):
    """공공 API의 한 페이지를 요청하고 XML 루트를 반환합니다."""
    params = {
        "serviceKey": api_key,
        "PageNo": page,
        "numOfrows": rows,
        "keyword": "",
        "gpsxfrom": "",
        "gpsyfrom": "",
        "gpsxto": "",
        "gpsyto": "",
        "serviceTp": "A",
        "from": start_date.strftime("%Y%m%d"),
        "to": end_date.strftime("%Y%m%d"),
    }

    try:
        response = requests.get(
            PUBLIC_EVENT_API_URL,
            params=params,
            timeout=20,
        )
        response.raise_for_status()
    except requests.RequestException:
        raise PublicEventAPIError(
            f"문화행사 공공 API {page}페이지 호출에 실패했습니다."
        ) from None

    try:
        root = ElementTree.fromstring(response.content)
    except ElementTree.ParseError as error:
        raise PublicEventAPIError(
            "문화행사 API의 XML 응답을 해석하지 못했습니다."
        ) from error

    result_code = _clean(root.findtext("./header/resultCode"))
    result_message = _clean(root.findtext("./header/resultMsg"))

    if result_code != "00":
        raise PublicEventAPIError(
            f"공공 API 오류: {result_message or result_code}"
        )

    return root


def fetch_public_events(
    start_date: date | None = None,
    end_date: date | None = None,
    rows: int = 100,
    max_pages: int = 10,
) -> list[dict]:
    """
    오늘부터 30일간 공연·문화행사를 조회하여
    기존 RSS 기사와 같은 형식으로 반환합니다.

    한 페이지당 최대 100건, 최대 10페이지까지 조회합니다.
    """
    api_key = unquote(
        os.getenv("PUBLIC_DATA_API_KEY", "").strip()
    )

    if not api_key:
        raise PublicEventAPIError(
            "PUBLIC_DATA_API_KEY 환경변수가 설정되지 않았습니다."
        )

    start_date = start_date or date.today()
    end_date = end_date or start_date + timedelta(days=30)

    if end_date < start_date:
        raise ValueError("end_date는 start_date보다 빠를 수 없습니다.")

    if rows < 1:
        raise ValueError("rows는 1 이상이어야 합니다.")

    if max_pages < 1:
        raise ValueError("max_pages는 1 이상이어야 합니다.")

    articles = []
    collected_at = datetime.now().isoformat(timespec="seconds")

    for page in range(1, max_pages + 1):
        root = _request_page(
            api_key=api_key,
            start_date=start_date,
            end_date=end_date,
            page=page,
            rows=rows,
        )

        items = root.findall("./body/items/item")

        if not items:
            break

        for item in items:
            sequence = _clean(item.findtext("seq"))
            title = _clean(item.findtext("title"))

            if not sequence or not title:
                continue

            service_name = _clean(
                item.findtext("serviceName"),
                default="문화행사",
            )
            start = _clean(
                item.findtext("startDate"),
                default="미정",
            )
            end = _clean(
                item.findtext("endDate"),
                default="미정",
            )
            place = _clean(
                item.findtext("place"),
                default="미정",
            )
            realm = _clean(
                item.findtext("realmName"),
                default="기타",
            )
            area = _clean(
                item.findtext("area"),
                default="미정",
            )
            sigungu = _clean(
                item.findtext("sigungu"),
            )
            thumbnail = _clean(
                item.findtext("thumbnail"),
            )

            # 전시·도서·체육 등 프로젝트 범위 밖 데이터 제외
            if not _is_allowed_realm(realm):
                continue

            event = {
                "title": title,
                "service_name": service_name,
                "start_date": start,
                "end_date": end,
                "place": place,
                "realm": realm,
                "area": area,
                "sigungu": sigungu,
                "thumbnail": thumbnail,
            }

            articles.append({
                "title": f"{title} 행사 일정",
                "url": _official_url(sequence, title),
                "published_at": collected_at,
                "source_name": SOURCE_NAME,
                "category": DEFAULT_CATEGORY,
                "content": _build_content(event),
            })

        total_count_text = _clean(
            root.findtext("./body/totalCount"),
            default="0",
        )

        try:
            total_count = int(total_count_text)
        except ValueError:
            total_count = 0

        # 마지막 페이지까지 조회했다면 종료
        if total_count and page * rows >= total_count:
            break

    return articles


# 터미널 출력용 (테스트) 추후, main 함수 전부 제거
def main():
    events = fetch_public_events(
        rows=100,
        max_pages=10,
    )

    print(f"문화포털 공연·문화행사 {len(events)}건 조회\n")

    for index, event in enumerate(events, start=1):
        print(f"[{index}] {event['title']}")
        print(event["content"])
        print(f"URL: {event['url']}")
        print("-" * 60)


if __name__ == "__main__":
    main()