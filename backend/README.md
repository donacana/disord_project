# Backend API

Python 3.10 이상. FastAPI + Neon PostgreSQL 기반 RAG API입니다.
기존 DB 테이블을 조회하며 스키마 생성/변경이나 기사 데이터 INSERT는 하지 않습니다.
질문 임베딩, pgvector cosine 검색, 검색 context 기반 답변 생성을 제공합니다.
기사 임베딩 적재, Bot, 수집 기능은 포함하지 않습니다.

## Windows 실행 (PowerShell)

```powershell
cd C:\jtkproject\discordproject\backend
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
notepad .env
```

`.env`의 DATABASE_URL에 Neon Connect에서 복사한 PostgreSQL 연결 문자열을 넣고 저장합니다.
SSL 옵션을 유지하고 비밀번호에 URL 예약 문자가 있으면 URL 인코딩합니다.
실제 비밀번호가 들어간 `.env`는 커밋하거나 공유하지 않습니다.
기존 환경변수 DATABASE_URL이 있으면 `.env`보다 우선합니다.

```powershell
uvicorn main:app --reload
```

PowerShell에서 활성화가 차단되면 실행 정책을 바꾸지 않고 아래처럼 실행할 수 있습니다.

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m uvicorn main:app --reload
```

CMD에서는 가상환경 활성화에 `.venv\Scripts\activate.bat`를 사용합니다.

Swagger: http://127.0.0.1:8000/docs
각 API에서 Try it out → Execute로 테스트합니다.

## API 테스트 (별도 PowerShell)

```powershell
Invoke-RestMethod http://127.0.0.1:8000/
Invoke-RestMethod http://127.0.0.1:8000/health
Invoke-RestMethod http://127.0.0.1:8000/stats
$body = @{ question = '최근 아이돌 이슈 알려줘'; top_k = 5 } | ConvertTo-Json
Invoke-RestMethod http://127.0.0.1:8000/ask -Method Post -ContentType 'application/json; charset=utf-8' -Body ([System.Text.Encoding]::UTF8.GetBytes($body))
```

- `/health`: SELECT 1 성공 시 HTTP 200과 status/api/database = ok. DB 실패 시 HTTP 503과 status=error, api=ok, database=error.
- `/stats`: total_articles, total_embeddings, last_collected_at, source_count 반환. 빈 테이블에서는 0, 0, null, 0입니다. DB/테이블 조회 실패는 HTTP 503입니다.
- `/ask`: 질문을 임베딩한 뒤 기존 article_embeddings를 cosine 검색하고, 검색 기사만 LLM context로 전달합니다. 결과 URL은 articles.url에서 반환합니다.
- 검색 결과가 없으면 `현재 수집된 자료에서 관련 정보를 찾지 못했습니다.`를 반환하고 LLM을 호출하지 않습니다.
- `OPENAI_CHAT_MODEL`은 기본값 `gpt-4o-mini`, `RAG_MIN_SIMILARITY`는 기본값 `0.3`입니다.
- question 누락/빈 문자열/공백만 입력, top_k 1~10 범위 밖 입력은 HTTP 422입니다. top_k 생략 시 5입니다.

기존 public.articles, public.article_embeddings, public.sources가 있어야 합니다.
테이블이 없거나 접속이 실패한 경우 0건으로 숨기지 않고 오류를 반환합니다.

## 검색 품질 개선 1단계

- `retrieval.py`에서 cosine 후보를 `top_k * 4`개(최대 40개) 조회하고 최종 `top_k`개만 선택합니다.
- 점수는 벡터 0.60 + 키워드 포함 비율 0.20 + 제목 포함 비율 0.15 + 최신성 0.05입니다.
- `query_utils.py`는 불용어·간단한 조사를 제거합니다. 별도 고유명사 사전이나 NLP 모델은 없으므로 이름 추출은 휴리스틱입니다.
- 최근/요즘/현재/근황/최신 질문에만 발행일 기준 7일 이내 1.0, 30일 이내 0.7, 90일 이내 0.3을 적용합니다. NULL 날짜는 0입니다.
- `RAG_MIN_SIMILARITY`는 가산점 적용 전 cosine 유사도 임계값입니다. 미달 후보는 제외하고 결과 수를 억지로 채우지 않습니다. 키워드 미포함만으로 제외하지 않습니다.
- 실제 샘플에서 0.45는 아이브 무관 기사를 줄였지만 영화 질문 결과를 모두 제거했고, 0.50은 아이브·장원영 결과도 제거했습니다. 따라서 기본값 0.3을 유지합니다. 기존 환경변수 설정이 우선하며 `.env`는 변경하지 않습니다.
- `RAG_DEBUG=true`이면 각 후보의 vector/keyword/title/recency/final 및 임계값 통과 여부를 로그로 출력합니다. 점수는 API 응답에 추가하지 않습니다. 기존 qa_logs 점수는 cosine 유사도를 유지합니다.
- 회귀 테스트: 프로젝트 루트에서 `backend/.venv/Scripts/python.exe -m unittest backend.test_retrieval`. DB/OpenAI를 모킹한 테스트이며 실서비스 연결 테스트와 구분합니다.
