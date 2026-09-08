"""
NAVER API HUB 뉴스 검색
담당: (본인 이름 적기)
"""

import os
import requests
from datetime import datetime


def fetch_naver_news(query: str, category: str, display: int = 100) -> list[dict]:
    headers = {
        "X-NCP-APIGW-API-KEY-ID": os.environ["NCLOUD_ACCESS_KEY"],
        "X-NCP-APIGW-API-KEY": os.environ["NCLOUD_SECRET_KEY"],
    }
    resp = requests.get(
        "https://naverapihub.apigw.ntruss.com/search/v1/news",
        headers=headers,
        params={"query": query, "display": display, "sort": "date"},
    )
    resp.raise_for_status()
    items = resp.json()["items"]

    articles = []
    for item in items:
        articles.append({
            "title": item["title"].replace("<b>", "").replace("</b>", ""),
            "url": item["originallink"] or item["link"],
            "published_at": item.get("pubDate") or datetime.now().isoformat(),
            "source_name": "네이버뉴스",
            "category": category,
        })
    return articles


NAVER_SEARCH_QUERIES = [
    # {"query": "TODO: 검색어", "category": "TODO"},
]


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv()
    for q in NAVER_SEARCH_QUERIES:
        results = fetch_naver_news(q["query"], q["category"])
        print(f"{q['query']}: {len(results)}건")