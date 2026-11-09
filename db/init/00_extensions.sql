-- ============================================================
-- AIMS - Industrial Analyzer Smart O&M Platform
-- Extensions required by the platform
-- ============================================================
CREATE EXTENSION IF NOT EXISTS timescaledb;
CREATE EXTENSION IF NOT EXISTS vector;      -- pgvector, for Knowledge / AI RAG embeddings
CREATE EXTENSION IF NOT EXISTS pgcrypto;    -- gen_random_uuid()
CREATE EXTENSION IF NOT EXISTS pg_trgm;     -- fuzzy text search (device/knowledge search)
