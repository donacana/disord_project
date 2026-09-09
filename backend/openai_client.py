import os
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI


load_dotenv(Path(__file__).with_name('.env'))


class OpenAIServiceError(RuntimeError):
    """Raised when an OpenAI request cannot be completed."""


def _client() -> OpenAI:
    api_key = os.getenv('OPENAI_API_KEY', '').strip()
    if not api_key:
        raise OpenAIServiceError('OPENAI_API_KEY 환경변수가 설정되지 않았습니다.')
    return OpenAI(api_key=api_key)


def embed_question(question: str) -> list[float]:
    try:
        response = _client().embeddings.create(
            model='text-embedding-3-small',
            input=question,
        )
        embedding = response.data[0].embedding
        if len(embedding) != 1536:
            raise OpenAIServiceError('질문 임베딩 차원이 1536이 아닙니다.')
        return embedding
    except OpenAIServiceError:
        raise
    except Exception as error:
        raise OpenAIServiceError('질문 임베딩 생성에 실패했습니다.') from error


def embed_articles(texts: list[str]) -> list[list[float]]:
    cleaned_texts = [
        text.strip()
        for text in texts
    ]

    if not cleaned_texts:
        raise OpenAIServiceError('임베딩할 기사 텍스트가 없습니다.')

    if any(not text for text in cleaned_texts):
        raise OpenAIServiceError('빈 기사 텍스트는 임베딩할 수 없습니다.')

    try:
        response = _client().embeddings.create(
            model='text-embedding-3-small',
            input=cleaned_texts,
        )

        ordered_data = sorted(
            response.data,
            key=lambda item: item.index,
        )

        embeddings = [
            item.embedding
            for item in ordered_data
        ]

        if len(embeddings) != len(cleaned_texts):
            raise OpenAIServiceError('기사 수와 임베딩 수가 일치하지 않습니다.')

        if any(len(embedding) != 1536 for embedding in embeddings):
            raise OpenAIServiceError('기사 임베딩 차원이 1536이 아닙니다.')

        return embeddings

    except OpenAIServiceError:
        raise
    except Exception as error:
        raise OpenAIServiceError('기사 임베딩 생성에 실패했습니다.') from error


def generate_answer(question: str, context: str) -> str:
    model = os.getenv('OPENAI_CHAT_MODEL', 'gpt-4o-mini').strip() or 'gpt-4o-mini'
    system_prompt = (
        '너는 연예·문화 뉴스 RAG Agent다.\n'
        '반드시 제공된 검색 자료만 근거로 답변한다. 검색 자료에 없는 사실을 추측하거나 만들어내지 않는다.\n'
        '각 주요 주장 뒤에는 해당 근거 기사 번호를 [1], [2] 형식으로 붙인다.\n'
        '질문의 핵심 인물/그룹/작품과 직접 관련 없는 자료는 사용하지 않는다.\n'
        '최근, 요즘, 현재 질문이면 최신 자료를 우선한다.\n'
        '사용하지 않은 출처 번호는 답변에 인용하지 않는다.\n'
        '서로 다른 기사에서 내용이 충돌하면 단정하지 말고 출처별 차이를 설명한다.\n'
        '자료에서 확인할 수 없는 질문이면 "현재 수집된 자료에서 확인하기 어렵습니다."라고 답한다.\n'
        '답변은 한국어로 작성한다.'
    )
    try:
        response = _client().chat.completions.create(
            model=model,
            temperature=0.2,
            messages=[
                {'role': 'system', 'content': system_prompt},
                {'role': 'user', 'content': f'질문:\n{question}\n\n검색 자료:\n{context}'},
            ],
        )
        answer = response.choices[0].message.content
        if not answer:
            raise OpenAIServiceError('LLM이 빈 답변을 반환했습니다.')
        return answer.strip()
    except OpenAIServiceError:
        raise
    except Exception as error:
        raise OpenAIServiceError('LLM 답변 생성에 실패했습니다.') from error
