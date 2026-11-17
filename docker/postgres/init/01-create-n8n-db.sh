#!/bin/bash
# Creates the n8n database alongside the main application database.
# Runs automatically on first postgres container start (docker-entrypoint-initdb.d).
set -euo pipefail

psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" <<-EOSQL
    SELECT 'CREATE DATABASE n8n'
    WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'n8n')\gexec
EOSQL

echo "n8n database ready"
