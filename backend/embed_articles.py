import argparse
import sys
from typing import Any

import db
from openai_client import OpenAIServiceError, embed_articles


EMBEDDING_MODEL = 'text-embedding-3-small'
MAX_EMBEDDING_CHARS = 12000
BATCH_SIZE = 10


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
        parts.append(
            f'요약: {summary}'
        )

    content = (article.get('content') or '').strip()

    if content:
        parts.append(
            f'본문: {content}'
        )

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
                        (
                            article['id'],
                        ),
                    )

                    saved_count += 1

        return saved_count

    except Exception as error:
        raise db.DatabaseError(
            '기사 임베딩 저장에 실패했습니다.'
        ) from error


def get_pending_count() -> int:
    row = db.fetch_one(
        PENDING_COUNT_QUERY
    )

    return int(
        row['count']
    ) if row else 0


def embed_single_article(
    article: dict,
) -> bool:
    """
    기사 1건만 개별 임베딩한다.
    성공하면 True, 실패하면 False.
    """

    try:
        text = build_embedding_text(
            article
        )

        if not text:
            print(
                f'[SKIP] article_id={article["id"]} '
                f'임베딩할 텍스트가 없습니다.'
            )
            return False

        embeddings = embed_articles(
            [text]
        )

        saved_count = save_embeddings(
            [article],
            embeddings,
        )

        if saved_count == 1:
            print(
                f'[개별 성공] '
                f'article_id={article["id"]}'
            )
            return True

        print(
            f'[개별 저장 실패] '
            f'article_id={article["id"]}'
        )

        return False

    except (
        db.DatabaseError,
        OpenAIServiceError,
    ) as error:
        print(
            f'[개별 실패 SKIP] '
            f'article_id={article["id"]} '
            f'오류={error}'
        )

        return False


def process_batch(
    batch_articles: list[dict],
    start_index: int,
    total_articles: int,
) -> tuple[int, int]:
    """
    배치 전체를 먼저 임베딩한다.

    배치 성공:
        그대로 저장.

    배치 실패:
        해당 배치만 기사별로 재시도한다.

    반환:
        (성공 건수, 실패 건수)
    """

    texts = [
        build_embedding_text(article)
        for article in batch_articles
    ]

    try:
        embeddings = embed_articles(
            texts
        )

        saved_count = save_embeddings(
            batch_articles,
            embeddings,
        )

        end_index = (
            start_index
            + len(batch_articles)
            - 1
        )

        print(
            f'[배치 성공] '
            f'{start_index}~{end_index} / '
            f'{total_articles} '
            f'저장={saved_count}건'
        )

        return (
            saved_count,
            len(batch_articles) - saved_count,
        )

    except (
        db.DatabaseError,
        OpenAIServiceError,
    ) as error:
        end_index = (
            start_index
            + len(batch_articles)
            - 1
        )

        print(
            f'[배치 실패] '
            f'{start_index}~{end_index} / '
            f'{total_articles}'
        )

        print(
            f'원인: {error}'
        )

        print(
            '해당 배치를 기사별로 다시 처리합니다.'
        )

        success_count = 0
        failed_count = 0

        for article in batch_articles:

            if embed_single_article(
                article
            ):
                success_count += 1
            else:
                failed_count += 1

        return (
            success_count,
            failed_count,
        )


def run(limit: int) -> None:
    articles = fetch_pending_articles(
        limit
    )

    if not articles:
        print(
            '임베딩할 기사가 없습니다.'
        )
        return

    total_articles = len(articles)
    total_saved = 0
    total_failed = 0

    print('=' * 60)
    print(
        f'임베딩 대상: {total_articles}건'
    )
    print(
        f'배치 크기: {BATCH_SIZE}건'
    )
    print('=' * 60)

    for start in range(
        0,
        total_articles,
        BATCH_SIZE,
    ):
        end = min(
            start + BATCH_SIZE,
            total_articles,
        )

        batch_articles = articles[
            start:end
        ]

        print()
        print(
            f'임베딩 요청: '
            f'{start + 1}~{end} / '
            f'{total_articles}'
        )

        saved_count, failed_count = (
            process_batch(
                batch_articles,
                start + 1,
                total_articles,
            )
        )

        total_saved += saved_count
        total_failed += failed_count

        print(
            f'현재 누적 저장: '
            f'{total_saved}건'
        )

        print(
            f'현재 누적 실패: '
            f'{total_failed}건'
        )

    try:
        pending_count = get_pending_count()
    except db.DatabaseError as error:
        print(
            f'남은 기사 수 조회 실패: {error}'
        )
        pending_count = -1

    print()
    print('=' * 60)
    print('기사 임베딩 처리 완료')
    print(
        f'임베딩 저장 완료: '
        f'{total_saved}건'
    )
    print(
        f'임베딩 실패/건너뜀: '
        f'{total_failed}건'
    )

    if pending_count >= 0:
        print(
            f'남은 미처리 기사: '
            f'{pending_count}건'
        )

    print('=' * 60)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            '미처리 기사의 임베딩을 생성합니다.'
        ),
    )

    parser.add_argument(
        '--limit',
        type=int,
        default=20,
        help=(
            '이번 실행에서 처리할 '
            '최대 기사 수'
        ),
    )

    return parser.parse_args()


if __name__ == '__main__':
    args = parse_args()

    if args.limit < 1:
        print(
            '--limit은 1 이상이어야 합니다.'
        )
        sys.exit(1)

    run(
        args.limit
    )
