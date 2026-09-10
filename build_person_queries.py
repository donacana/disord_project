"""
DB에 이미 쌓인 articles.keywords에서 자주 등장하는 인물/그룹명을 뽑아
네이버 검색어 목록으로 만든다.

    python build_person_queries.py

사람이 이름을 일일이 예측해서 넣는 대신,
실제 뉴스에 등장한 인물을 자동으로 뽑아 보강하는 방식.
출력 결과를 sources_naver.py의 NAVER_SEARCH_QUERIES에 붙여넣으면 된다.
"""

import os
from collections import Counter

import psycopg
from dotenv import load_dotenv

# 인물명이 아닌 일반명사 (keyword_extractor의 STOPWORDS로 못 거른 것들)
EXTRA_STOPWORDS = {
    "행사", "일정", "공연", "콘서트", "축제", "페스티벌", "정기연주회", "독주회",
    "리사이틀", "오케스트라", "합창단", "교향악단", "예술의전당", "문화예술회관",
    "국립극장", "세종문화회관", "롯데콘서트홀", "클래식", "피아노", "바이올린",
    "서울", "부산", "대구", "대전", "광주", "인천", "울산", "경기", "제주",
    "한국", "일본", "미국", "중국", "글로벌", "월드", "정규", "미니", "싱글",
    "티저", "예고", "공식", "인터뷰", "포토", "종합", "속보", "단독",
}

MIN_COUNT = 2   # 최소 이 횟수 이상 등장한 이름만 채택
TOP_N = 60      # 상위 몇 명까지 뽑을지

load_dotenv()
conn = psycopg.connect(os.environ["DATABASE_URL"])
cur = conn.cursor()

# keywords 배열을 펼쳐서 빈도 집계
cur.execute("""
    SELECT unnest(keywords) AS kw, count(*) AS cnt
    FROM articles
    WHERE keywords IS NOT NULL
    GROUP BY kw
    ORDER BY cnt DESC
""")

counter = Counter()
for kw, cnt in cur.fetchall():
    kw = kw.strip()
    if kw in EXTRA_STOPWORDS:
        continue
    if len(kw) < 2 or len(kw) > 10:
        continue
    # 숫자 포함, 특수문자 포함 제외
    if any(c.isdigit() for c in kw):
        continue
    counter[kw] = cnt

top = [(kw, cnt) for kw, cnt in counter.most_common(TOP_N) if cnt >= MIN_COUNT]

print(f"=== 빈출 인물/키워드 상위 {len(top)}개 ===\n")
for kw, cnt in top:
    print(f"  {kw}: {cnt}건")

print("\n=== 아래를 sources_naver.py의 NAVER_SEARCH_QUERIES에 추가하세요 ===\n")
for kw, cnt in top:
    print(f'    {{"query": "{kw}", "category": "celeb"}},')

conn.close()
