FROM python:3.11-slim

WORKDIR /srv/app

# Only copy dependency manifests first so dependency installation is cached across
# rebuilds that only touch application code.
COPY pyproject.toml ./
RUN pip install --no-cache-dir uvicorn[standard] fastapi asyncpg sqlglot pydantic \
    pydantic-settings apscheduler opentelemetry-api opentelemetry-sdk google-adk

COPY app ./app

EXPOSE 8000

HEALTHCHECK --interval=10s --timeout=5s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/healthz', timeout=3)" || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
