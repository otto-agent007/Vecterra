#!/usr/bin/env bash
# Apply the schema to a scratch PostGIS database and run the RLS assertions.
# Usage: DATABASE_URL=postgres://... backend/tests/sql/run.sh
# In CI the URL points at the postgis service container; locally at any
# throwaway Postgres 15+ with PostGIS installed. Never run this against a
# real Supabase project — the prelude creates stand-in auth/storage schemas.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
: "${DATABASE_URL:?set DATABASE_URL to a scratch PostGIS database}"

PGOPTIONS="-c client_min_messages=warning" psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -q -f "$here/ci_prelude.sql"
PGOPTIONS="-c client_min_messages=warning" psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -q -f "$here/../../ingestion/schema.sql"
PGOPTIONS="-c client_min_messages=warning" psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -q -o /dev/null -f "$here/rls_assertions.sql"
