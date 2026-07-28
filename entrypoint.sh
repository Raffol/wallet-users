#!/bin/sh
# Старт контейнера: миграции, затем сервер
set -e

cd "$(dirname "$0")"

if [ ! -f /app/alembic.ini ]; then
    echo "ERROR: /app/alembic.ini not found inside the image."
    echo "Contents of /app:"
    ls -la /app
    echo "Make sure the whole project (incl. alembic.ini and the"
    echo "alembic/ folder) is present in the Docker build context."
    exit 1
fi

echo "Applying database migrations..."
alembic -c /app/alembic.ini upgrade head

echo "Starting API server on :8000..."
exec uvicorn app.main:app --host 0.0.0.0 --port 8000
