CREATE TABLE IF NOT EXISTS trend_briefs (
    id            BIGSERIAL PRIMARY KEY,
    domain        TEXT NOT NULL DEFAULT 'culture',
    category      TEXT,
    period_start  TIMESTAMPTZ NOT NULL,
    period_end    TIMESTAMPTZ NOT NULL,
    summary_text  TEXT NOT NULL,
    article_count INT NOT NULL,
    source_urls   JSONB NOT NULL,
    generated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_trend_briefs_generated
    ON trend_briefs (generated_at DESC);