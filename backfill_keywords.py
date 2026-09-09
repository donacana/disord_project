"""
이미 저장된 기사 중 keywords가 비어 있는 것들에 키워드를 채워 넣는다.
    python backfill_keywords.py

한 번만 돌리면 되고, 이후 새로 수집되는 기사는 collector.py가 자동으로 채운다.
여러 번 실행해도 이미 채워진 기사는 건너뛰므로 안전하다.
"""

import os
import sys

import psycopg
from dotenv import load_dotenv

# collector 폴더의 keyword_extractor를 import
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "collector"))
from keyword_extractor import extract_keywords

load_dotenv()
conn = psycopg.connect(os.environ["DATABASE_URL"])

with conn:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, title FROM articles
            WHERE keywords IS NULL OR array_length(keywords, 1) IS NULL
            """
        )
        rows = cur.fetchall()

    print(f"keywords 비어있는 기사 {len(rows)}건 발견, 채우는 중...")

    filled, empty = 0, 0
    for i, (article_id, title) in enumerate(rows, 1):
        kws = extract_keywords(title)
        if not kws:
            empty += 1
            continue

        with conn.cursor() as cur:
            cur.execute(
                "UPDATE articles SET keywords = %s WHERE id = %s",
                (kws, article_id),
            )
        filled += 1

        if i % 200 == 0:
            print(f"  [{i}/{len(rows)}] 진행 중...")

print(f"\n완료: {filled}건 채움 / {empty}건은 추출 결과 없음")
conn.close()
