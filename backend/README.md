# Backend API

Python 3.10 이상. FastAPI + Neon PostgreSQL 연결 골격입니다.
기존 DB 테이블을 조회하며 스키마 생성/변경이나 데이터 INSERT는 하지 않습니다.
실제 RAG, OpenAI, 임베딩 검색, Bot, 수집 기능은 포함하지 않습니다.

## Windows 실행 (PowerShell)

```powershell
cd C:\jtkproject\9-8\backend
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
- `/ask`: 임시 답변, sources=[], domain=ent_culture. DB 연결 없이도 동작합니다.
- question 누락/빈 문자열/공백만 입력, top_k 1~10 범위 밖 입력은 HTTP 422입니다. top_k 생략 시 5입니다.

기존 public.articles, public.article_embeddings, public.sources가 있어야 합니다.
테이블이 없거나 접속이 실패한 경우 0건으로 숨기지 않고 오류를 반환합니다.
