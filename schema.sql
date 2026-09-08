-- =====================================================================
--  연예·문화 RAG Agent — DB 스키마
--  PostgreSQL 16 + pgvector
--
--  이 파일이 팀의 단일 진실 소스(single source of truth)입니다.
--  컬럼 추가/변경은 반드시 이 파일을 고치고 #team 채널에 공지하세요.
--
--  적용:  psql "$DATABASE_URL" -f schema.sql
-- =====================================================================

CREATE EXTENSION IF NOT EXISTS vector;   -- 임베딩 검색
CREATE EXTENSION IF NOT EXISTS pg_trgm;  -- 한글 부분일치 키워드 검색


-- ---------------------------------------------------------------------
-- 1. sources : 수집 소스 목록 + 신뢰도 등급
--    연예 도메인은 루머/추측성 기사가 많아 소스 등급이 답변 톤을 좌우함
--    (tier 1 = 공식 발표, 2 = 주요 언론, 3 = 연예 전문 매체)
--    산출물 "수집 소스 목록 + 약관 확인"의 근거 자료로도 씀
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sources (
    id          SERIAL PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,          -- '연합뉴스', 'Google News', ...
    feed_url    TEXT,                          -- RSS/API 엔드포인트
    site_url    TEXT,
    tier        SMALLINT NOT NULL DEFAULT 2
                CHECK (tier BETWEEN 1 AND 3),
    tos_checked BOOLEAN NOT NULL DEFAULT FALSE, -- 이용약관/robots.txt 확인 여부
    tos_note    TEXT,
    is_active   BOOLEAN NOT NULL DEFAULT TRUE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);


-- ---------------------------------------------------------------------
-- 2. categories : 카테고리 참조 테이블
--    code 자체가 PK(자연키). articles.category 에 'music' 문자열이 그대로
--    들어가므로 일반 검색에는 조인이 필요 없음.
--    한글 라벨/이모지가 필요한 /brief 응답에서만 조인해서 씀.
--
--    ※ is_active = FALSE 는 "예술팀 영역이라 의도적으로 제외" 표시.
--       예술팀이 없다면 TRUE 로 바꾸기만 하면 바로 활성화됨.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS categories (
    code       TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    description TEXT,
    is_active  BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

INSERT INTO categories (code, label_ko, emoji, sort_order, is_active, note) VALUES
    ('music',   '음악',        '🎵', 1, TRUE,  '아이돌·가수, 음원·앨범, 차트, 컴백'),
    ('drama',   '드라마',      '📺', 2, TRUE,  '방영·캐스팅·시청률·종영'),
    ('show',    '예능·방송',   '🎬', 3, TRUE,  '예능 프로그램, 출연진'),
    ('celeb',   '연예인 소식', '⭐', 4, TRUE,  '배우, 기획사, 열애·결혼, 논란'),
    ('event',   '공연·시상식', '🎤', 5, TRUE,  '콘서트, 팬미팅, 시상식, 투어, 티켓'),
    ('movie',   '영화',        '🍿', 6, TRUE,  '개봉·박스오피스·흥행 (작품 해설 제외)'),
    ('webtoon', '웹툰·IP',     '📚', 7, TRUE,  '웹툰·웹소설, 원작 영상화'),
    ('trend',   '온라인 화제', '🔥', 8, TRUE,  '밈, 유행, 화제성'),
    -- 아래 3개는 예술팀 영역. 예술팀이 없으면 is_active 를 TRUE 로.
    ('stage',      '공연·뮤지컬', '🎭', 20, FALSE, '뮤지컬·연극·클래식 (예술팀 영역)'),
    ('exhibition', '전시',        '🖼️', 21, FALSE, '미술관·박물관 전시 (예술팀 영역)'),
    ('book',       '도서·문학',   '📖', 22, FALSE, '신간·문학상·작가 (예술팀 영역)')
ON CONFLICT (code) DO NOTHING;


-- ---------------------------------------------------------------------
-- 3. articles : 수집한 기사 원문
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS articles (
    id            BIGSERIAL PRIMARY KEY,

    -- 도메인/분류 --------------------------------------------------
    domain        TEXT NOT NULL DEFAULT 'culture',
    category      TEXT,          -- categories.code 참조 (FK 는 Day3 에 추가)
    keywords      TEXT[],        -- 제목에서 뽑은 인물·작품명 (하이브리드 검색용)

    -- 본문 ---------------------------------------------------------
    title         TEXT NOT NULL,
    content       TEXT NOT NULL,
    summary       TEXT,          -- /brief 에서 필수. LLM 요약 또는 content[:200]

    -- 출처 ---------------------------------------------------------
    url           TEXT NOT NULL,
    source_id     INT REFERENCES sources(id),
    source_name   TEXT NOT NULL, -- 조인 없이 바로 쓰려고 비정규화 (답변 출처 표기용)

    -- 중복 판정 ----------------------------------------------------
    url_hash      TEXT NOT NULL UNIQUE,   -- 정규화 URL(utm_* 제거)의 sha256
    content_hash  TEXT,                   -- 본문 sha256. 제목만 바꾼 재탕 기사 탐지

    -- 시간 ---------------------------------------------------------
    published_at  TIMESTAMPTZ,            -- 기사 발행 시각 (RSS pubDate)
    collected_at  TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- 상태 ---------------------------------------------------------
    is_embedded   BOOLEAN NOT NULL DEFAULT FALSE,  -- 임베딩 완료 플래그
    char_len      INT GENERATED ALWAYS AS (length(content)) STORED
);

-- 최신순 정렬/필터 (연예는 최신성이 생명)
CREATE INDEX IF NOT EXISTS idx_articles_published
    ON articles (published_at DESC NULLS LAST);
CREATE INDEX IF NOT EXISTS idx_articles_collected
    ON articles (collected_at DESC);

-- 카테고리 필터
CREATE INDEX IF NOT EXISTS idx_articles_category
    ON articles (domain, category);

-- 키워드 배열 검색: keywords @> ARRAY['뉴진스']
CREATE INDEX IF NOT EXISTS idx_articles_keywords
    ON articles USING gin (keywords);

-- 한글 부분일치: title ILIKE '%아이유%'
CREATE INDEX IF NOT EXISTS idx_articles_title_trgm
    ON articles USING gin (title gin_trgm_ops);

-- 임베딩 대기열 조회용 (수집기가 자주 씀)
CREATE INDEX IF NOT EXISTS idx_articles_pending
    ON articles (id) WHERE is_embedded = FALSE;


-- ---------------------------------------------------------------------
-- 4. article_embeddings : pgvector 저장소
--    모델 고정: text-embedding-3-small / 1536차원
--    ※ 모델을 바꾸면 전체 재임베딩이므로 팀 합의 없이 변경 금지
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS article_embeddings (
    article_id BIGINT PRIMARY KEY
               REFERENCES articles(id) ON DELETE CASCADE,
    embedding  vector(1536) NOT NULL,
    model      TEXT NOT NULL DEFAULT 'text-embedding-3-small',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 코사인 거리 인덱스. 데이터 수천 건까지는 없어도 되지만 만들어 둬도 무해함
CREATE INDEX IF NOT EXISTS idx_embeddings_hnsw
    ON article_embeddings
    USING hnsw (embedding vector_cosine_ops);


-- ---------------------------------------------------------------------
-- 5. entities : 인물·그룹·작품 (graphDB 역할, Neo4j 대신 Postgres로 구현)
--    이유: 이 규모(수백~수천 건)에서 Neo4j 컨테이너를 새로 띄우는 비용이
--    "인물·사건 관계"라는 기능 하나가 주는 가치보다 큼.
--    수집 시 자동 추출은 부담이니 LLM으로 배치 추출(Day 3, 여유 있을 때)
--    하거나, articles.keywords 배열에서 등장 빈도로 상위 N개만 뽑아도 됨.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS entities (
    id          BIGSERIAL PRIMARY KEY,
    name        TEXT NOT NULL,               -- '아이유', '뉴진스', '기생충'
    entity_type TEXT NOT NULL                -- person / group / work / agency
                CHECK (entity_type IN ('person','group','work','agency')),
    aliases     TEXT[],                      -- 이명·영문명 (검색 매칭용)
    mention_count INT NOT NULL DEFAULT 0,    -- 언급 기사 수 (집계 캐시)
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (name, entity_type)
);

CREATE INDEX IF NOT EXISTS idx_entities_name_trgm
    ON entities USING gin (name gin_trgm_ops);


-- ---------------------------------------------------------------------
-- 6. entity_relations : 엔티티 간 관계 + 근거 기사
--    그래프의 "간선(edge)"에 해당. article_id 가 근거(출처)이므로
--    관계 하나도 반드시 기사로 뒷받침됨 (환각 방지 원칙과 동일)
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS entity_relations (
    id            BIGSERIAL PRIMARY KEY,
    from_entity_id BIGINT NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    to_entity_id   BIGINT NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    relation_type  TEXT NOT NULL,            -- 'collaborated_with' / 'member_of' /
                                              -- 'starred_in' / 'produced_by' 등
    article_id     BIGINT REFERENCES articles(id) ON DELETE SET NULL,  -- 근거
    confidence     REAL,                     -- (선택) 추출 신뢰도
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (from_entity_id, to_entity_id, relation_type, article_id)
);

CREATE INDEX IF NOT EXISTS idx_relations_from
    ON entity_relations (from_entity_id);
CREATE INDEX IF NOT EXISTS idx_relations_to
    ON entity_relations (to_entity_id);


-- ---------------------------------------------------------------------
-- 7. trend_briefs : 트렌드 브리핑 스냅샷
--    /brief 는 실시간 계산만 하면 그날의 결과가 안 남음.
--    Day2 vs Day4 브리핑을 나란히 비교하는 게 "매일 갱신" 의 가장 확실한
--    증거이므로, 생성될 때마다 결과를 남겨서 재사용·비교 가능하게 함.
--    수집 워크플로우 끝에 자동 생성해도 되고(Day3 이후), 그때까지는
--    누군가 /brief 를 부를 때 INSERT 하는 것만으로도 충분함.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS trend_briefs (
    id            BIGSERIAL PRIMARY KEY,
    domain        TEXT NOT NULL DEFAULT 'culture',
    category      TEXT REFERENCES categories(code),  -- NULL = 전체 카테고리 종합
    period_start  TIMESTAMPTZ NOT NULL,   -- 집계 범위 시작 (예: now() - 7일)
    period_end    TIMESTAMPTZ NOT NULL,
    summary_text  TEXT NOT NULL,          -- LLM이 생성한 브리핑 본문
    article_count INT NOT NULL,           -- 이 브리핑이 근거한 기사 수
    source_urls   JSONB NOT NULL,         -- [{"title":..., "url":..., "published_at":...}, ...]
    generated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_trend_briefs_generated
    ON trend_briefs (generated_at DESC);
CREATE INDEX IF NOT EXISTS idx_trend_briefs_category
    ON trend_briefs (category, generated_at DESC);


-- ---------------------------------------------------------------------
-- 8. qa_logs : Discord/API 질의응답 기록
--    Day 4 회고에서 "어떤 질문에 약했나" 분석 근거
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS qa_logs (
    id            BIGSERIAL PRIMARY KEY,
    question      TEXT NOT NULL,
    answer        TEXT,
    sources_json  JSONB,        -- [{title, url, collected_at, score}, ...]
    top_score     REAL,         -- 검색 최고 유사도 (임계값 튜닝에 씀)
    is_answered   BOOLEAN NOT NULL DEFAULT TRUE,  -- FALSE = "자료 없음" 응답
    latency_ms    INT,
    channel_id    TEXT,
    user_id       TEXT,
    asked_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_qa_logs_asked
    ON qa_logs (asked_at DESC);


-- ---------------------------------------------------------------------
-- 9. collection_runs : 수집 실행 이력
--    "매일 갱신했다"의 증거 자료 (수집 품질 20점)
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS collection_runs (
    id             BIGSERIAL PRIMARY KEY,
    started_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at    TIMESTAMPTZ,
    fetched_count  INT NOT NULL DEFAULT 0,  -- 소스에서 가져온 건수
    inserted_count INT NOT NULL DEFAULT 0,  -- 실제 신규 저장 건수
    duplicate_count INT NOT NULL DEFAULT 0, -- 중복으로 버린 건수
    embedded_count INT NOT NULL DEFAULT 0,
    status         TEXT NOT NULL DEFAULT 'running', -- running/success/failed
    error_message  TEXT
);


-- ---------------------------------------------------------------------
-- 10. service_heartbeats : 각 서비스의 "현재" 상태  ★ /health 의 핵심
--    api / bot / collector 가 살아있을 때 주기적으로 UPSERT
--    행이 5개 이하로 유지되는 작은 테이블 (append 아님)
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS service_heartbeats (
    component    TEXT PRIMARY KEY,   -- 'api' | 'bot' | 'collector' | 'llm' | 'embedding'
    status       TEXT NOT NULL DEFAULT 'ok'
                 CHECK (status IN ('ok', 'degraded', 'down')),
    detail       TEXT,               -- 실패 사유, 버전 정보 등
    latency_ms   INT,
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 각 서비스가 기동/주기적으로 실행:
--   INSERT INTO service_heartbeats (component, status, latency_ms)
--   VALUES ('bot', 'ok', 42)
--   ON CONFLICT (component) DO UPDATE
--     SET status = EXCLUDED.status,
--         latency_ms = EXCLUDED.latency_ms,
--         detail = EXCLUDED.detail,
--         last_seen_at = now();

-- 초기 행 등록 (없으면 /health 가 컴포넌트를 아예 인지 못함)
INSERT INTO service_heartbeats (component, status, detail)
VALUES ('api', 'down', '미기동'),
       ('bot', 'down', '미기동'),
       ('collector', 'down', '미실행')
ON CONFLICT (component) DO NOTHING;


-- ---------------------------------------------------------------------
-- 11. health_checks : 상태 점검 이력 (append-only)
--    "언제 죽었다 살아났나"를 발표/회고에서 보여주는 용도
--    ※ /health 를 폴링하면 빠르게 커지므로 매 호출마다 넣지 말 것.
--       상태가 '바뀔 때'만 기록하거나 5분에 1회로 제한.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS health_checks (
    id         BIGSERIAL PRIMARY KEY,
    component  TEXT NOT NULL,
    status     TEXT NOT NULL
               CHECK (status IN ('ok', 'degraded', 'down')),
    latency_ms INT,
    detail     TEXT,
    checked_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_health_checks_recent
    ON health_checks (component, checked_at DESC);

-- 7일치만 유지 (수집 잡에 같이 끼워 넣으면 편함)
--   DELETE FROM health_checks WHERE checked_at < now() - interval '7 days';


-- ---------------------------------------------------------------------
-- 12. 뷰 : 엔드포인트가 한 줄로 읽어가는 집계
-- ---------------------------------------------------------------------

-- GET /stats
CREATE OR REPLACE VIEW v_stats AS
SELECT
    (SELECT count(*) FROM articles)                            AS total_articles,
    (SELECT count(*) FROM article_embeddings)                  AS total_embeddings,
    (SELECT count(*) FROM articles
      WHERE collected_at >= now() - interval '24 hours')       AS collected_last_24h,
    (SELECT max(collected_at) FROM articles)                   AS last_collected_at,
    (SELECT count(DISTINCT source_name) FROM articles)         AS source_count,
    (SELECT count(*) FROM qa_logs)                             AS total_questions,
    (SELECT finished_at FROM collection_runs
      WHERE status = 'success'
      ORDER BY finished_at DESC LIMIT 1)                       AS last_run_at;

-- GET /health : 컴포넌트별 상태 + 자동 stale 판정
--   2분 넘게 하트비트가 없으면 스스로 죽었다고 간주
CREATE OR REPLACE VIEW v_health AS
SELECT
    component,
    CASE
        WHEN last_seen_at < now() - interval '2 minutes' THEN 'down'
        ELSE status
    END                                                        AS status,
    detail,
    latency_ms,
    last_seen_at,
    EXTRACT(EPOCH FROM (now() - last_seen_at))::INT             AS seconds_since_seen
FROM service_heartbeats
ORDER BY component;


-- =====================================================================
--  참고: 자주 쓰는 쿼리
-- =====================================================================

-- [수집] 중복이면 조용히 무시 (수집기를 몇 번 돌려도 안전)
--   INSERT INTO articles (domain, category, title, content, url, url_hash,
--                         content_hash, source_name, published_at)
--   VALUES (...)
--   ON CONFLICT (url_hash) DO NOTHING
--   RETURNING id;

-- [검색] 벡터 + 키워드 하이브리드 (가중합 0.7 : 0.3)
--   SELECT a.id, a.title, a.url, a.source_name, a.published_at, a.collected_at,
--          left(a.content, 800) AS snippet,
--          (1 - (e.embedding <=> $1)) * 0.7
--          + (CASE WHEN a.title ILIKE '%' || $2 || '%' THEN 1 ELSE 0 END) * 0.3
--            AS score
--   FROM articles a
--   JOIN article_embeddings e ON e.article_id = a.id
--   WHERE a.domain = 'culture'
--     AND ($3::timestamptz IS NULL OR a.published_at >= $3)  -- "이번 주" 같은 질문
--   ORDER BY score DESC
--   LIMIT $4;

-- [임베딩 대기열]
--   SELECT id, title, content FROM articles
--   WHERE is_embedded = FALSE ORDER BY id LIMIT 100;

-- [헬스체크] DB 자체 생존은 테이블 없이 이걸로 충분
--   SELECT 1;


-- [브리핑 - 원본 조회] /brief 계산용 원문 (시간 필터, 카테고리 조인은 여기서만)
--   SELECT c.emoji, c.label_ko, a.title, a.summary, a.url, a.published_at
--   FROM articles a
--   LEFT JOIN categories c ON c.code = a.category
--   WHERE a.domain = 'culture'
--     AND a.published_at >= now() - interval '7 days'
--   ORDER BY c.sort_order NULLS LAST, a.published_at DESC
--   LIMIT 40;

-- [브리핑 - 저장] LLM 요약 후 스냅샷으로 남기기
--   INSERT INTO trend_briefs
--     (domain, category, period_start, period_end, summary_text, article_count, source_urls)
--   VALUES
--     ('culture', NULL, now() - interval '7 days', now(),
--      $1, $2, $3::jsonb)
--   RETURNING id, generated_at;

-- [브리핑 - 캐시 재사용] 오늘 이미 만든 브리핑이 있으면 재사용 (같은 카테고리, 6시간 이내)
--   SELECT summary_text, source_urls, generated_at FROM trend_briefs
--   WHERE domain = 'culture'
--     AND category IS NOT DISTINCT FROM $1
--     AND generated_at >= now() - interval '6 hours'
--   ORDER BY generated_at DESC LIMIT 1;

-- [Day2 vs Day4 비교] 발표 자료용
--   SELECT generated_at, article_count, left(summary_text, 200)
--   FROM trend_briefs
--   WHERE category IS NULL
--   ORDER BY generated_at;


-- [관계 그래프] "A와 B의 관계는?" 질문에 씀
--   SELECT e2.name, r.relation_type, a.title, a.url, a.published_at
--   FROM entities e1
--   JOIN entity_relations r
--     ON r.from_entity_id = e1.id OR r.to_entity_id = e1.id
--   JOIN entities e2
--     ON e2.id = CASE WHEN r.from_entity_id = e1.id THEN r.to_entity_id
--                      ELSE r.from_entity_id END
--   LEFT JOIN articles a ON a.id = r.article_id
--   WHERE e1.name = '아이유'
--   ORDER BY a.published_at DESC NULLS LAST
--   LIMIT 10;

-- [엔티티 언급 랭킹] mention_count 캐시 갱신 (수집 배치 끝에 실행)
--   UPDATE entities e SET mention_count = sub.cnt
--   FROM (
--       SELECT unnest(keywords) AS name, count(*) AS cnt
--       FROM articles WHERE published_at >= now() - interval '30 days'
--       GROUP BY 1
--   ) sub
--   WHERE e.name = sub.name;


-- =====================================================================
--  Day 3 마이그레이션 : 카테고리 목록이 굳은 뒤에 실행
--  (Day 2 에 미리 걸면 새 카테고리 추가할 때마다 흐름이 끊김)
-- =====================================================================
-- 1) 미등록 값 확인 -- 0건이어야 함
--   SELECT DISTINCT category FROM articles
--   WHERE category IS NOT NULL
--     AND category NOT IN (SELECT code FROM categories);
--
-- 2) 나온 게 있으면 categories 에 추가하거나 오타 정리
--   UPDATE articles SET category = 'music' WHERE category = 'Music';
--
-- 3) FK 제약 추가 -- 이후 오타는 INSERT 단계에서 거부됨
--   ALTER TABLE articles
--       ADD CONSTRAINT fk_articles_category
--       FOREIGN KEY (category) REFERENCES categories(code)
--       ON UPDATE CASCADE;


-- =====================================================================
--  확장 메모 (나중에 필요하면)
-- =====================================================================
-- ▸ 기사가 길어 검색 정확도가 떨어지면 청크 테이블 추가.
--   articles 는 안 건드려도 되게 설계돼 있음.
--
--   CREATE TABLE article_chunks (
--       id         BIGSERIAL PRIMARY KEY,
--       article_id BIGINT NOT NULL REFERENCES articles(id) ON DELETE CASCADE,
--       chunk_idx  INT NOT NULL,
--       chunk_text TEXT NOT NULL,
--       embedding  vector(1536) NOT NULL,
--       UNIQUE (article_id, chunk_idx)
--   );
--
-- ▸ 컬럼 추가는 언제든 안전:  ALTER TABLE articles ADD COLUMN thumbnail_url TEXT;
-- ▸ 전체 초기화:  TRUNCATE articles CASCADE;   (embeddings 도 같이 지워짐)