#!/bin/bash
# Creates the hardened, non-superuser role the agent connects with.
# Password is injected via env var (never hardcoded) - supports either
# POSTGRES_AGENT_PASSWORD directly or POSTGRES_AGENT_PASSWORD_FILE (docker secret).
set -euo pipefail

if [ -n "${POSTGRES_AGENT_PASSWORD_FILE:-}" ]; then
  AGENT_PW="$(cat "$POSTGRES_AGENT_PASSWORD_FILE")"
elif [ -n "${POSTGRES_AGENT_PASSWORD:-}" ]; then
  AGENT_PW="$POSTGRES_AGENT_PASSWORD"
else
  echo "FATAL: neither POSTGRES_AGENT_PASSWORD nor POSTGRES_AGENT_PASSWORD_FILE is set" >&2
  exit 1
fi

if [ -n "${POSTGRES_REFRESHER_PASSWORD_FILE:-}" ]; then
  REFRESHER_PW="$(cat "$POSTGRES_REFRESHER_PASSWORD_FILE")"
elif [ -n "${POSTGRES_REFRESHER_PASSWORD:-}" ]; then
  REFRESHER_PW="$POSTGRES_REFRESHER_PASSWORD"
else
  echo "FATAL: neither POSTGRES_REFRESHER_PASSWORD nor POSTGRES_REFRESHER_PASSWORD_FILE is set" >&2
  exit 1
fi

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
  -v agent_pw="$AGENT_PW" -v refresher_pw="$REFRESHER_PW" <<-'EOSQL'
    -- Zero implicit rights: no superuser, no CREATEDB, no privilege inheritance from PUBLIC.
    CREATE ROLE agent_ro WITH LOGIN PASSWORD :'agent_pw' NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION;

    -- The normalized core schema (raw facts/dims, incl. anything PII-adjacent) is never
    -- reachable by the agent. Only the pre-computed, PII-scrubbed semantic layer in
    -- `public` is. This REVOKE is defense-in-depth: agent_ro is never granted core access
    -- in the first place, but stating it explicitly documents the intent and survives
    -- future schema changes that might otherwise grant PUBLIC access accidentally.
    REVOKE ALL ON SCHEMA core FROM agent_ro;
    REVOKE ALL ON SCHEMA core FROM PUBLIC;
    GRANT USAGE ON SCHEMA public TO agent_ro;

    -- Database-level runtime safety net, independent of anything the application does.
    ALTER ROLE agent_ro SET default_transaction_read_only = on;
    ALTER ROLE agent_ro SET statement_timeout = '8s';
    ALTER ROLE agent_ro SET idle_in_transaction_session_timeout = '15s';
    ALTER ROLE agent_ro SET work_mem = '32MB';

    -- Separate role for the background MV-refresh job: also non-superuser, but needs
    -- write access to refresh materialized views and the refresh_log tracking table.
    -- Kept distinct from agent_ro so a compromised query path can never trigger a refresh
    -- or write anywhere.
    CREATE ROLE refresher_rw WITH LOGIN PASSWORD :'refresher_pw' NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION;
    GRANT USAGE ON SCHEMA public TO refresher_rw;
EOSQL
