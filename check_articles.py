from dotenv import load_dotenv
import os, psycopg

load_dotenv()
conn = psycopg.connect(os.environ["DATABASE_URL"])
cur = conn.cursor()

cur.execute("SELECT count(*) FROM articles")
total = cur.fetchone()[0]
cur.execute("SELECT count(*) FROM article_embeddings")
embedded = cur.fetchone()[0]
print(f"전체 기사: {total}건 / 임베딩: {embedded}건 ({embedded*100//total}%)")

print("\n=== 카테고리별 ===")
cur.execute("SELECT category, count(*) FROM articles GROUP BY category ORDER BY 2 DESC")
for row in cur.fetchall():
    print(f"  {row[0]}: {row[1]}건")

print("\n=== 소스별 ===")
cur.execute("SELECT source_name, count(*) FROM articles GROUP BY source_name ORDER BY 2 DESC")
for row in cur.fetchall():
    print(f"  {row[0]}: {row[1]}건")

print("\n=== keywords 채워진 기사 ===")
cur.execute("SELECT count(*) FROM articles WHERE keywords IS NOT NULL AND array_length(keywords, 1) > 0")
print(f"  {cur.fetchone()[0]}건")