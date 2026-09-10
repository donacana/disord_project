# add_fk.py
from dotenv import load_dotenv
import os, psycopg

load_dotenv()
conn = psycopg.connect(os.environ["DATABASE_URL"])
with conn, conn.cursor() as cur:
    cur.execute("""
        ALTER TABLE articles
            ADD CONSTRAINT fk_articles_category
            FOREIGN KEY (category) REFERENCES categories(code)
            ON UPDATE CASCADE
    """)
print("FK 제약 추가 완료")