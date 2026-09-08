"""
sources_naver.py가 요구하는 sources.name='네이버뉴스' 행을 미리 만들어둠.
sources_naver.py는 이 행이 없으면 에러를 내므로, 한 번만 실행해두면 됨.
    python add_naver_source.py
"""
from dotenv import load_dotenv
import os, psycopg

load_dotenv()
conn = psycopg.connect(os.environ["DATABASE_URL"])
with conn, conn.cursor() as cur:
    cur.execute(
        "INSERT INTO sources (name) VALUES (%s) ON CONFLICT (name) DO NOTHING",
        ("네이버뉴스",),
    )
print("완료: sources 테이블에 '네이버뉴스' 등록 확인")