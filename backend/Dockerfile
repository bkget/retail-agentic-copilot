FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /srv/app

# Dependencies come from pyproject.toml (single source of truth) and are installed
# before the app code is copied, so code-only changes reuse the cached layer.
# EXTRAS selects optional providers (comma-separated): "redis" (default) and "gemini"
# (Google ADK - large; only needed for LLM_PROVIDER=gemini: BACKEND_EXTRAS=redis,gemini).
ARG EXTRAS="redis"
COPY pyproject.toml ./
RUN mkdir -p app && touch app/__init__.py \
    && pip install ".[${EXTRAS}]" \
    && pip uninstall -y agentic-data-copilot-backend \
    && rm -rf app build *.egg-info

COPY app ./app

# Never run the API as root.
RUN useradd --system --uid 10001 --no-create-home appuser
USER appuser

EXPOSE 8000

HEALTHCHECK --interval=10s --timeout=5s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/healthz', timeout=3)" || exit 1

# --timeout-graceful-shutdown lets in-flight SSE answers finish on deploy/restart.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--timeout-graceful-shutdown", "20"]
