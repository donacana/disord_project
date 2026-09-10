from dotenv import load_dotenv
import os, psycopg

load_dotenv()
conn = psycopg.connect(os.environ["DATABASE_URL"])
cur = conn.cursor()

cur.execute("SELECT code, name FROM categories ORDER BY code")
rows = cur.fetchall()
print(f"categories 테이블 총 {len(rows)}건:")
for row in rows:
    print(row)