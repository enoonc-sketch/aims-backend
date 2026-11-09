# ===== AIMS Database image: PostgreSQL 16 + TimescaleDB + pgvector =====
FROM postgres:16-bookworm

ENV TIMESCALEDB_VERSION=2.17.2

# Install TimescaleDB (official apt repo) and pgvector (Debian/PGDG package)
RUN apt-get update && apt-get install -y --no-install-recommends \
        gnupg2 \
        curl \
        ca-certificates \
        lsb-release \
    && curl -fsSL https://packagecloud.io/timescale/timescaledb/gpgkey | gpg --dearmor -o /usr/share/keyrings/timescaledb-archive-keyring.gpg \
    && echo "deb [signed-by=/usr/share/keyrings/timescaledb-archive-keyring.gpg] https://packagecloud.io/timescale/timescaledb/debian/ $(lsb_release -cs) main" \
        > /etc/apt/sources.list.d/timescaledb.list \
    && apt-get update && apt-get install -y --no-install-recommends \
        timescaledb-2-postgresql-16 \
        postgresql-16-pgvector \
    && apt-get purge -y --auto-remove gnupg2 curl \
    && rm -rf /var/lib/apt/lists/* \
    && sed -ri "s/^#?shared_preload_libraries.*/shared_preload_libraries = 'timescaledb'/" /usr/share/postgresql/postgresql.conf.sample || true

-- Init SQL scripts run in alphabetical order on first container start.
-- Only 00_extensions.sql remains here on purpose: table/column DDL used to
-- be duplicated by hand in 01_schema.sql (and demo data in 02_seed.sql),
-- which drifted from the real schema (app/models/*.py + migrations/). The
-- API container already creates every table from those ORM models on
-- startup (AUTO_INIT_DB=true, see app/db/init_db.py:create_tables) and adds
-- the Postgres-only extras (TimescaleDB hypertable, pgvector index) that
-- ORM DDL can't express — so this directory only needs to enable the
-- Postgres extensions that CREATE TABLE / those extras depend on before the
-- API ever connects.
COPY init/ /docker-entrypoint-initdb.d/
