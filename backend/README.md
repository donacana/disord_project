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

## 답변 신뢰성 검증

검색/임베딩/재정렬과 API 요청·응답 schema는 유지합니다. 검색 결과가 있으면 다음 순서로 처리합니다.

1. 기존 OpenAI chat 모델로 근거 제한 프롬프트를 사용해 초안을 생성합니다.
2. `answer_validator.py`가 문장별 citation 범위, 숫자·단위·날짜·따옴표 안 명칭 및 구체적 단어를 확인합니다.
3. 같은 `OPENAI_CHAT_MODEL`로 최종 검증을 1회 호출합니다. 주체·행위·시점·질문 의도가 실제 근거와 맞는지 확인하고, 기사 원문 문장을 최대 3개 선택하도록 요청합니다.
4. 검증 출력에도 규칙 검사를 다시 적용합니다. 최종 사실 문장은 인용한 기사 제목 전체 또는 본문의 완전한 문장과 일치해야 합니다. 의역의 모든 구체적 토큰이 원문 한 문장에 함께 있으면 그 원문으로 교체하고, 여러 문장에 흩어져 있거나 근거가 없으면 제거합니다. 공백·따옴표 모양·끝 문장부호 차이만 허용하며 주어 변경이나 문장 조합은 허용하지 않습니다. 최종 답변은 최대 3문장입니다.
5. 상세 설명이 모두 탈락했지만 모델이 인용한 기사 제목에 질문 entity·intent가 직접 나타나면 그 제목만 보수적으로 사용합니다. 예를 들어 근거 없는 개봉일은 제거하고 원문 제목의 개봉 소식만 반환할 수 있습니다. 모델이 명시적으로 확인 불가라고 판정하면 이 fallback도 하지 않습니다.
6. 통과한 문장에 사용된 기사만 `sources`에 남기고 답변 citation을 `[1]`부터 다시 매깁니다. qa_logs에도 최종 답변·사용 출처를 기록합니다. 기존 score/top_score는 cosine 유사도입니다.

검증 근거는 모델에게 실제로 보낸 기사 제목과 요약 우선/본문 대체 최대 4000자입니다.
발행일·URL·전달하지 않은 본문은 날짜나 활동 이력을 입증하는 근거로 사용하지 않습니다.
각 기사 context에는 번호·제목·출처·발행일·본문·URL과 시작/끝 구분을 유지합니다.

잘못된/누락된 인용, 근거 없는 구체적 토큰, 원문과 다른 문장은 제거합니다.
확인 불가라는 전반적 결론이 사실 문장과 함께 나오면 보수적으로 근거 부족 응답으로 통일합니다.
검증 서비스 오류나 통과 문장 부재 시 미검증 초안을 반환하지 않고 `현재 수집된 자료만으로는 확인하기 어렵습니다.`와 빈 sources를 반환합니다.
이 경우 qa_logs의 is_answered=false, top_score=null입니다. 질문 임베딩/초안 생성 오류의 기존 503 처리는 유지합니다.

검색 결과가 있으면 생성 1회 + 검증 1회로 지연과 비용이 늘어납니다. 검증 재시도 루프나 별도 모델은 추가하지 않았습니다.
`RAG_DEBUG=true`일 때 삭제 문장 수와 사용 원본 citation 번호만 추가 기록하며 초안 전문은 로그에 출력하지 않습니다.

이 정책은 자유로운 요약보다 정확성을 우선하므로 올바른 의역도 제거할 수 있습니다.
원문 인용 자체가 질문에 적절한지, 원문 정보가 사실인지까지 수학적으로 보장하지는 않으며 의미 적합성은 모델 검증에 의존합니다.
관계나 시점 해석이 어려우면 답변을 줄이거나 보류합니다.

회귀 테스트: `backend/.venv/Scripts/python.exe -m unittest backend.test_retrieval backend.test_answer_validator`.
실제 다섯 질문의 검색 기사·초안·모델 검증·최종 답변·삭제 문장·인용 비교는 기존 [비교 보고서](SEARCH_QUALITY_REPORT.md)의 답변 신뢰성 절에 기록합니다.
