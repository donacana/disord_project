import os

from openai import OpenAI


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


def generate_answer(question: str, context: str) -> str:
    model = os.getenv('OPENAI_CHAT_MODEL', 'gpt-4o-mini').strip() or 'gpt-4o-mini'
    system_prompt = (
        '너는 연예·문화 뉴스 RAG Agent다.\n'
        '반드시 제공된 검색 자료만 근거로 답변한다. 검색 자료에 없는 사실을 추측하거나 만들어내지 않는다.\n'
        '각 주요 주장 뒤에는 해당 근거 기사 번호를 [1], [2] 형식으로 붙인다.\n'
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
