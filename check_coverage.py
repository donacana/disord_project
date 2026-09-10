# check_coverage.py
from dotenv import load_dotenv
import os, psycopg

load_dotenv()
conn = psycopg.connect(os.environ["DATABASE_URL"])
cur = conn.cursor()

# 특정 인물이 keywords에 몇 번 등장하는지
for name in ["아이유", "장원영", "임영웅", "뉴진스", "BTS"]:
    cur.execute(
        "SELECT count(*) FROM articles WHERE %s = ANY(keywords)",
        (name,)
    )
    print(f"{name}: {cur.fetchone()[0]}건")