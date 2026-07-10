FROM ghcr.io/astral-sh/uv:python3.14-bookworm-slim

WORKDIR /app

# The workspace members must be present before sync so uv can build them; the
# packages/*/pyproject.toml files and their sources are all under packages/.
COPY pyproject.toml uv.lock ./
COPY packages ./packages
RUN uv sync --frozen --no-dev

ENV LDP_HOST=0.0.0.0 \
    LDP_TLS_MODE=terminated \
    LDP_STORAGE_ROOT=/data \
    PATH="/app/.venv/bin:$PATH"

VOLUME /data
EXPOSE 8000

CMD ["python", "-m", "ldp_pod"]
