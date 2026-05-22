#!/bin/sh
set -e

echo "Waiting for PostgreSQL to become available..."
python - <<'PY'
import os
import time

import psycopg2

host = os.getenv("DATABASE_HOST", "db")
port = int(os.getenv("DATABASE_PORT", "5432"))
name = os.getenv("DATABASE_NAME", "gutendex")
user = os.getenv("DATABASE_USER", "gutendex")
password = os.getenv("DATABASE_PASSWORD", "gutendex")
timeout = int(os.getenv("DB_WAIT_TIMEOUT", "120"))

start = time.time()
while True:
    try:
        conn = psycopg2.connect(
            host=host,
            port=port,
            dbname=name,
            user=user,
            password=password,
        )
        conn.close()
        break
    except psycopg2.OperationalError:
        if time.time() - start >= timeout:
            raise SystemExit("Database connection timeout reached")
        time.sleep(1)
PY

echo "Applying migrations..."
python manage.py migrate --noinput

echo "Collecting static files..."
python manage.py collectstatic --noinput

if [ "${RUN_UPDATECATALOG_ON_STARTUP:-false}" = "true" ]; then
  echo "Updating Gutenberg catalog (this can take several minutes)..."
  python manage.py updatecatalog
fi

echo "Starting Django server..."
exec python manage.py runserver 0.0.0.0:8000
