from dotenv import load_dotenv
import os, psycopg

load_dotenv()
conn = psycopg.connect(os.environ["DATABASE_URL"])
cur = conn.cursor()

cur.execute("""
    SELECT DISTINCT category FROM articles
    WHERE category IS NOT NULL
      AND category NOT IN (SELECT code FROM categories)
""")
result = cur.fetchall()
print("등록 안 된 category 값:", result)

if not result:
    print("→ 0건입니다. FK 제약 걸어도 안전합니다.")