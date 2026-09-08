# check_articles.py
from dotenv import load_dotenv
import os, psycopg

load_dotenv()
conn = psycopg.connect(os.environ["DATABASE_URL"])
cur = conn.cursor()
cur.execute("SELECT count(*) FROM articles")
print("전체 기사 수:", cur.fetchone()[0])
cur.execute("SELECT category, count(*) FROM articles GROUP BY category ORDER BY 2 DESC")
print(cur.fetchall())