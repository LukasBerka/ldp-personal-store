FROM ghcr.io/astral-sh/uv:python3.14-bookworm-slim

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

COPY ldp_personal_store ./ldp_personal_store

ENV LDP_HOST=0.0.0.0 \
    LDP_TLS_MODE=terminated \
    LDP_STORAGE_ROOT=/data \
    PATH="/app/.venv/bin:$PATH"

VOLUME /data
EXPOSE 8000

CMD ["python", "-m", "ldp_personal_store.main"]
