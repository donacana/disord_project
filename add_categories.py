from dotenv import load_dotenv
import os, psycopg

load_dotenv()
conn = psycopg.connect(os.environ["DATABASE_URL"])

CATEGORIES = [
    ("music", "음악", "아이돌·가수, 음원·앨범, 차트, 컴백"),
    ("drama", "드라마", "방영·캐스팅·시청률·종영"),
    ("show", "예능·방송", "예능 프로그램, 출연진"),
    ("celeb", "연예인 소식", "배우, 기획사, 열애·결혼, 논란"),
    ("event", "공연·시상식", "콘서트, 팬미팅, 시상식, 투어, 티켓, 문화행사"),
    ("movie", "영화", "개봉·박스오피스·흥행"),
    ("webtoon", "웹툰·IP", "웹툰·웹소설, 원작 영상화"),
    ("trend", "온라인 화제", "밈, 유행, 화제성"),
]

with conn, conn.cursor() as cur:
    for code, name, description in CATEGORIES:
        cur.execute(
            """
            INSERT INTO categories (code, name, description, is_active)
            VALUES (%s, %s, %s, TRUE)
            ON CONFLICT (code) DO NOTHING
            """,
            (code, name, description),
        )
print("categories 8개 등록 완료")