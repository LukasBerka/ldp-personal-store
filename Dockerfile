FROM ghcr.io/astral-sh/uv:python3.14-bookworm-slim

WORKDIR /app

# The project is not itself a package (uv sync installs only the dependencies), so the
# lockfile is enough to build the dependency layer before the source is copied in.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

COPY ldp_common ./ldp_common
COPY ldp_personal_store ./ldp_personal_store
COPY ldp_view_engine ./ldp_view_engine

ENV LDP_HOST=0.0.0.0 \
    LDP_TLS_MODE=terminated \
    LDP_STORAGE_ROOT=/data \
    PATH="/app/.venv/bin:$PATH"

VOLUME /data
EXPOSE 8000

CMD ["python", "-m", "ldp_personal_store.main"]
