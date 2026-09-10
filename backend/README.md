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
- 검색 결과가 없으면 `현재 수집된 자료만으로는 확인하기 어렵습니다.`를 반환하고 LLM을 호출하지 않습니다.
- `OPENAI_CHAT_MODEL`은 기본값 `gpt-4o-mini`, `RAG_MIN_SIMILARITY`는 기본값 `0.3`입니다.
- question 누락/빈 문자열/공백만 입력, top_k 1~10 범위 밖 입력은 HTTP 422입니다. top_k 생략 시 5입니다.

기존 public.articles, public.article_embeddings, public.sources가 있어야 합니다.
테이블이 없거나 접속이 실패한 경우 0건으로 숨기지 않고 오류를 반환합니다.

## 검색 품질 개선 2단계

`retrieval.py`는 cosine 후보를 한 번만 `top_k * 4`개(최대 40개) 조회합니다.
`query_utils.py`에서 불용어·조사를 제거해 entity 후보와 규칙 기반 intent를 추출합니다.
별도 인물 사전, LLM 분석, 추가 검색, 임베딩 변경은 없습니다.

- `config.py` 가중치: vector 0.42 / keyword 0.14 / title 0.105 / recency 0.035 / entity 0.15 / intent 0.10 / category 0.05. 1단계 비율을 70%로 유지하고 새 관련성 신호에 30%를 배정했습니다.
- entity는 제목·본문에서 확인합니다. intent는 제목 일치 1.0, 본문만 일치 0.4입니다. category는 일치 가산점이며 영화·개봉·음악 발매·드라마처럼 명확한 의도에서만 불일치 감점합니다. 제목 의도가 맞으면 오분류 가능성을 고려해 감점을 줄입니다.
- 활동·광고·행사·발매·개봉 질문에서 제목이 법적 사건/논란이고 긍정적 intent 단어가 제목에 없으면 제외합니다. 단어 규칙이라 복합 기사나 잘린 제목은 오판할 수 있습니다.
- `RAG_MIN_SIMILARITY` 기본값 0.3과 환경변수 우선순위를 유지합니다. 정확한 entity 제목 일치와 충분한 최종 점수가 있으면 임계값보다 최대 0.10 낮은 후보까지 구제합니다(유사도 하한 0.20). `.env`는 수정하지 않습니다.
- 최종 점수 하한은 0.15, entity 직접 포함 후보는 0.35입니다. entity 미포함 후보는 유사도 0.65 이상, 제목 intent와 category 일치, 최종 점수 0.40 이상을 모두 충족해야 합니다. 미포함 결과는 최대 1개이며 직접 포함 결과가 있으면 과반을 유지합니다. 결과 수를 억지로 채우지 않습니다.
- 최근/요즘/현재/최신/근황/이번달은 30일, 이번주(띄어쓰기 허용)는 7일을 사용합니다. 관련성 통과 → 직접 관련성 우선 → 기간 내 우선 → 부족하면 이전/날짜 미상 기사 순서로 선택하며 추가 DB 검색은 하지 않습니다. NULL published_at은 기간 점수 0이고 collected_at으로 대체하지 않습니다. naive 날짜는 UTC로 해석합니다.
- 동일 article_id, 동일 URL, 제목의 Unicode·대소문자·공백·문장부호 정규화 후 완전 일치 중복을 제거합니다. 같은 사건의 다른 제목은 유지합니다. 동점이면 collected_at이 최신인 기사를 우선합니다.
- 동일 source_name은 2개를 우선 상한으로 삼습니다. 같은 관련성·기간 그룹에서 점수 차이 0.08 이내의 다른 출처가 있을 때만 먼저 선택하고, 대안이 부족하거나 관련성이 낮으면 상한을 완화합니다. `네이버뉴스`처럼 통합된 출처 메타데이터는 실제 언론사별로 구분할 수 없습니다.
- `RAG_DEBUG=true`일 때만 후보 ID·제목(80자 이내)·vector/entity/intent/category/recency/final 점수·출처·적격 여부를 출력합니다. API에는 점수를 추가하지 않으며 qa_logs는 기존 cosine 점수 기록을 유지합니다.

회귀 테스트: 프로젝트 루트에서 `backend/.venv/Scripts/python.exe -m unittest backend.test_retrieval`.
실제 후보의 1·2단계 비교는 [SEARCH_QUALITY_REPORT.md](SEARCH_QUALITY_REPORT.md)를 참고하세요.

## 질문 이해와 답변 생성

모든 `/ask` 요청은 먼저 LLM Query Understanding을 거칩니다. 결과는 `entity`, `intent`, `time_range`, `target_type`, `keywords`, `normalized_question`, `search_queries`, `confidence`를 포함합니다. 분석 장애 또는 낮은 확신도일 때만 규칙 분석을 사용합니다. 질문 분석에서 사용한 사전지식은 답변 근거에 포함하지 않습니다.

- `trend_ranking`: `trend_service.aggregate()`로 이동하며 임베딩/vector 검색을 호출하지 않습니다.
- 그 외: 기존 RAG 검색 및 관련성 기준을 유지합니다. 활동·컴백은 근거가 충분하면 4~6문장, 정의는 2~4문장, 논란은 3~5문장을 목표로 합니다. 광범위한 질문은 결론, 주요 포인트, 최근 흐름을 설명합니다.

### 트렌드 집계

최근 질문은 기본 7일 동안 발행되어 현재 DB에 수집된 기사 중 최신 1,000건을 조회합니다. 미래 발행일은 제외합니다. 이는 전체 기사나 대중 인기도의 전수조사가 아닙니다.

기사 제목과 역할 설명을 포함한 본문 발췌를 묶어서 실제 인물/그룹의 기사별 언급과 역할을 추출합니다. 반환된 이름과 기사 번호가 해당 DB 발췌에 존재하는지 확인합니다. 이름이 일반 단어와 겹쳐도 본문 전체의 문자열 검색으로 언급량을 늘리지 않습니다. 한 기사에서 확인된 역할은 동일 인물의 다른 기사별 언급에도 적용합니다. 공백이 있는 그룹명과 기존 명확한 별칭을 보존/정규화합니다.

각 묶음은 40개 기사, 최대 4개 동시 요청, 개별 추출 제한 시간은 35초입니다. 같은 발췌 묶음의 성공 결과는 메모리에 캐시합니다. 추출 실패 시 제목의 대상과 본문의 명시적 연예인 역할이 함께 확인되는 경우만 보수적으로 복구합니다. 일부 묶음이 실패하면 답변에 부분 집계임을 명시합니다.

기사 URL/hash 기준으로 중복을 제거하고 기사 언급량, 서로 다른 URL 도메인 수, 최근성을 기존 가중치 0.60/0.25/0.15로 집계합니다. `네이버뉴스`처럼 여러 언론사를 합친 수집원 이름을 하나의 언론사로 계산하지 않습니다. 후보 1개만 있어도 답하고, 반복 언급 후보가 없으면 1회 언급 후보를 사용합니다.

집계 대상 목록과 대상별 근거 번호를 대표 기사 본문과 함께 전달합니다. 생성 모델이 대표 기사의 다른 등장인물을 순위에 추가하지 않도록 구분합니다. 최종 답변에는 실제 집계로 만든 직접 결론과 기간·표본·순위 범위를 항상 유지합니다. 동일 기사 URL은 출처 목록에서 합치고 인용 번호를 다시 매깁니다.

### 문장별 검증

같은 설정 모델로 각 문장의 주체·행위·시점·부정 여부와 근거를 검증하고, `text`, `citations`, `section`을 가진 구조화된 문장 배열을 받습니다. 문단 끝에만 인용이 있던 초안도 검증 단계에서 문장마다 근거를 지정합니다. 인용한 여러 기사가 함께 뒷받침하는 통합 설명은 허용하며, 일부 문장이 근거 없으면 해당 문장만 삭제합니다.

로컬 검증기는 인용 형식·번호, 숫자와 단위, 인용된 고유명사 등을 검사합니다. 의미 검증을 통과한 문장에 원문과 모든 단어가 동일해야 한다는 조건을 재적용하지 않습니다. 의미 검증이 실패한 경우에는 직접 관련된 원문 문장/제목만 복구할 수 있고, 단순한 단어 겹침만으로 거절된 초안을 복원하지 않습니다. 근거가 없는 문장뿐일 때만 확인 불가로 처리합니다. 트렌드는 생성/검증 실패 시 실제 집계 수치와 기사 제목으로 부분 답변합니다.

일반 RAG의 검증 근거는 모델에 보낸 제목과 요약 우선/본문 대체 최대 4,000자입니다. 전달하지 않은 본문이나 질문 분석 내용은 사실 근거가 아닙니다. API 응답의 `answer`, `sources`, `domain` 및 DB 스키마, 수집기, 임베딩은 변경하지 않았습니다. Discord는 답변을 요약하지 않고 길이에 맞게 나누어 모두 표시합니다. 최초 집계 요청을 기다릴 수 있도록 봇의 `API_TIMEOUT_SECONDS` 기본값은 180초이며 환경변수로 조정할 수 있습니다.

### 검증과 진단

프로젝트 루트에서 실행합니다.

```powershell
backend/.venv/Scripts/python.exe -m unittest discover -s backend -p 'test_*.py'
backend/.venv/Scripts/python.exe -m backend.evaluate_answer_quality --output backend/answer_quality_results.json
```

두 번째 명령은 실제 `.env`의 DB/OpenAI에 요청하며 기존 `qa_logs`에도 결과를 기록합니다. `--question`을 반복 지정하면 특정 질문만 평가합니다.

`RAG_DEBUG=true`이면 `[ANSWER_TRACE]` JSON 로그에 질문 분석, 경로, 집계/검색 후보 수, 상위 대상, 생성 답변, 검증 답변, 최종 답변, 부족 사유를 출력합니다. 이 정보는 API 계약에 추가되지 않습니다. 실제 실행 결과와 한계는 [ANSWER_QUALITY_REPORT.md](ANSWER_QUALITY_REPORT.md)를 참고하세요.
