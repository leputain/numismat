FROM python:3.14.7-slim-bookworm
ARG APP_VERSION="0.45.0"
LABEL org.opencontainers.image.title="Numismat" \
      org.opencontainers.image.version="${APP_VERSION}" \
      org.opencontainers.image.source="https://github.com/leputain/numismat"
COPY --from=ghcr.io/astral-sh/uv:0.12.2 /uv /uvx /bin/
WORKDIR /app
RUN apt-get update \
    && apt-get install --yes --no-install-recommends \
        tesseract-ocr=5.3.0-2 \
        tesseract-ocr-eng=1:4.1.0-2 \
        tesseract-ocr-rus=1:4.1.0-2 \
    && rm -rf /var/lib/apt/lists/*
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
COPY alembic.ini ./
COPY migrations ./migrations
RUN useradd --uid 10001 --create-home finbot
USER 10001:10001
ENV PATH="/app/.venv/bin:$PATH" PYTHONPATH=/app/src
CMD ["python", "-m", "finbot"]
