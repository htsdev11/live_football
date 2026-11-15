#!/usr/bin/env bash
# Create the PostgreSQL role and database for this project.
#
# Needs an administrative Postgres connection, which is why it is a script you
# run rather than something the app does: the role has to exist before Django
# can connect at all.
#
#   ./scripts/setup_postgres.sh                 # uses the defaults below
#   DB_PASSWORD='something-else' ./scripts/setup_postgres.sh
#
# Afterwards, export the same password for Django:
#
#   export POSTGRES_PASSWORD='...'
#
# CREATEDB is granted because `manage.py test` builds its own test database.

set -euo pipefail

DB_NAME="${DB_NAME:-football}"
DB_USER="${DB_USER:-football}"
DB_PASSWORD="${DB_PASSWORD:-football@0099}"

echo "Creating role '${DB_USER}' and database '${DB_NAME}'..."

sudo -u postgres psql -v ON_ERROR_STOP=1 <<SQL
DO \$\$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '${DB_USER}') THEN
        CREATE ROLE ${DB_USER} LOGIN PASSWORD '${DB_PASSWORD}' CREATEDB;
        RAISE NOTICE 'role ${DB_USER} created';
    ELSE
        ALTER ROLE ${DB_USER} LOGIN PASSWORD '${DB_PASSWORD}' CREATEDB;
        RAISE NOTICE 'role ${DB_USER} updated';
    END IF;
END
\$\$;
SQL

if sudo -u postgres psql -tAc \
    "SELECT 1 FROM pg_database WHERE datname='${DB_NAME}'" | grep -q 1; then
    echo "database ${DB_NAME} already exists"
else
    sudo -u postgres createdb -O "${DB_USER}" "${DB_NAME}"
    echo "database ${DB_NAME} created"
fi

echo
echo "Done. Now run:"
echo "  export POSTGRES_PASSWORD='${DB_PASSWORD}'"
echo "  .venv/bin/python manage.py migrate"
