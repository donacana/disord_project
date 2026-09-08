import argparse
import sys
from typing import Any

import db
from openai_client import OpenAIServiceError, embed_articles


EMBEDDING_MODEL = 'text-embedding-3-small'
MAX_EMBEDDING_CHARS = 12000

PENDING_ARTICLES_QUERY = """
SELECT
    a.id,
    a.title,
    a.summary,
    a.content,
    a.keywords
FROM public.articles AS a
LEFT JOIN public.article_embeddings AS ae
    ON ae.article_id = a.id
WHERE a.is_embedded = FALSE
   OR ae.article_id IS NULL
ORDER BY a.id
LIMIT %s
"""

SAVE_EMBEDDING_QUERY = """
INSERT INTO public.article_embeddings (
    article_id,
    embedding,
    model,
    created_at
)
VALUES (%s, %s::vector, %s, NOW())
ON CONFLICT (article_id)
DO UPDATE SET
    embedding = EXCLUDED.embedding,
    model = EXCLUDED.model,
    created_at = NOW()
"""

MARK_EMBEDDED_QUERY = """
UPDATE public.articles
SET is_embedded = TRUE
WHERE id = %s
"""

PENDING_COUNT_QUERY = """
SELECT COUNT(*) AS count
FROM public.articles AS a
LEFT JOIN public.article_embeddings AS ae
    ON ae.article_id = a.id
WHERE a.is_embedded = FALSE
   OR ae.article_id IS NULL
"""


def vector_literal(values: list[float]) -> str:
    return '[' + ','.join(
        str(value)
        for value in values
    ) + ']'


def build_embedding_text(article: dict[str, Any]) -> str:
    parts = [
        f'제목: {article["title"]}',
    ]

    keywords = article.get('keywords') or []
    if keywords:
        parts.append(
            '키워드: ' + ', '.join(keywords)
        )

    summary = (article.get('summary') or '').strip()
    if summary:
        parts.append(f'요약: {summary}')

    content = (article.get('content') or '').strip()
    if content:
        parts.append(f'본문: {content}')

    text = '\n'.join(parts).strip()
    return text[:MAX_EMBEDDING_CHARS]


def fetch_pending_articles(limit: int) -> list[dict]:
    return db.fetch_all(
        PENDING_ARTICLES_QUERY,
        (limit,),
    )


def save_embeddings(
    articles: list[dict],
    embeddings: list[list[float]],
) -> int:
    saved_count = 0

    try:
        with db.get_connection() as connection:
            with connection.cursor() as cursor:
                for article, embedding in zip(
                    articles,
                    embeddings,
                    strict=True,
                ):
                    cursor.execute(
                        SAVE_EMBEDDING_QUERY,
                        (
                            article['id'],
                            vector_literal(embedding),
                            EMBEDDING_MODEL,
                        ),
                    )
                    cursor.execute(
                        MARK_EMBEDDED_QUERY,
                        (article['id'],),
                    )
                    saved_count += 1

        return saved_count

    except Exception as error:
        raise db.DatabaseError(
            '기사 임베딩 저장에 실패했습니다.'
        ) from error


def get_pending_count() -> int:
    row = db.fetch_one(PENDING_COUNT_QUERY)
    return int(row['count']) if row else 0


def run(limit: int) -> None:
    articles = fetch_pending_articles(limit)

    if not articles:
        print('임베딩할 기사가 없습니다.')
        return

    print(f'임베딩 대상: {len(articles)}건')

    texts = [
        build_embedding_text(article)
        for article in articles
    ]

    try:
        embeddings = embed_articles(texts)
        saved_count = save_embeddings(
            articles,
            embeddings,
        )
    except (
        db.DatabaseError,
        OpenAIServiceError,
    ) as error:
        print(f'임베딩 처리 실패: {error}')
        sys.exit(1)

    pending_count = get_pending_count()

    print(f'임베딩 저장 완료: {saved_count}건')
    print(f'남은 미처리 기사: {pending_count}건')


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description='미처리 기사의 임베딩을 생성합니다.',
    )
    parser.add_argument(
        '--limit',
        type=int,
        default=20,
        help='한 번에 처리할 최대 기사 수',
    )
    return parser.parse_args()


if __name__ == '__main__':
    args = parse_args()

    if args.limit < 1:
        print('--limit은 1 이상이어야 합니다.')
        sys.exit(1)

    run(args.limit)
