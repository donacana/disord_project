"""
추가 RSS 소스 목록
담당: (본인 이름 적기)

여기에 새로 찾은 RSS 주소만 추가하세요.
collector.py의 다른 부분(본문 추출, 저장, 필터)은 건드리지 않아도 됩니다.

── 지켜야 할 것 ──────────────────────────────────────
1. 이미 쓰고 있는 소스와 겹치지 않게 (연합뉴스, 한국경제, 스포츠경향은 이미 사용 중)
2. 범위가 너무 넓은 "전체 뉴스" RSS는 피하기
3. 추가한 소스는 직접 한 번 실행해보고, 무관한 기사가 안 섞이는지 확인 후 공유
4. category는 다음 중 하나로: music / drama / show / celeb / event / movie / webtoon
─────────────────────────────────────────────────
"""

EXTRA_RSS_SOURCES = [
    # 아래 형식 그대로 추가하세요. 예시:
    # {"source_name": "언론사이름", "url": "RSS주소", "category": "celeb"},

    {
        "source_name": "SBS",
        "url": "https://news.sbs.co.kr/news/SectionRssFeed.do?sectionId=14&plink=RSSREADER",
        "category": "celeb",
    },
    {
        "source_name": "조선일보",
        "url": "https://www.chosun.com/arc/outboundfeeds/rss/category/entertainments/?outputType=xml",
        "category": "celeb",
    },
{
        "source_name": "뉴시스",
        "url": "https://www.newsis.com/RSS/entertain.xml",
        "category": "celeb",
    },
    {
        "source_name": "국민일보",
        "url": "https://www.kmib.co.kr/rss/data/kmibEntRss.xml",
        "category": "celeb",
    },
]