#!/bin/sh
set -e

echo "Initializing database..."
python - <<'PY'
import os
from app.database import engine, Base

# Create all tables
Base.metadata.create_all(bind=engine)
print("Database tables created successfully")
PY

if [ "${RUN_UPDATECATALOG_ON_STARTUP:-false}" = "true" ]; then
  echo "Updating Gutenberg catalog (this can take several minutes)..."
  python catalog/updatecatalog.py
fi

echo "Starting FastAPI server..."
exec uvicorn app.main:app --host 0.0.0.0 --port 8000
