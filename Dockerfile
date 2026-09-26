# syntax=docker/dockerfile:1

# ---------- base: dependências de runtime ----------
FROM python:3.11-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install .

COPY alembic.ini ./
COPY migrations ./migrations

RUN useradd --create-home --uid 1000 app && chown -R app /app
USER app

# ---------- runtime: imagem da API ----------
FROM base AS runtime
EXPOSE 8000
# Aplica migrações e sobe a API. Em caso de falha na migração, o container não inicia.
CMD ["sh", "-c", "alembic upgrade head && exec uvicorn product_intelligence.main:app --host 0.0.0.0 --port 8000"]

# ---------- dev: testes e lint ----------
FROM base AS dev
USER root
RUN pip install ".[dev]"
COPY tests ./tests
USER app
CMD ["pytest"]
