FROM ghcr.io/astral-sh/uv:python3.14-bookworm-slim

WORKDIR /app

# Layer the locked dependency install separately from the source so code edits
# do not invalidate the dependency cache.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

COPY ldp_personal_store ./ldp_personal_store

# A container is reached over a virtual network interface, never its own
# loopback, so the pod must bind 0.0.0.0; tls_mode=off refuses exactly that
# bind, so TLS is declared terminated by whatever fronts the published port.
ENV LDP_HOST=0.0.0.0 \
    LDP_TLS_MODE=terminated \
    LDP_STORAGE_ROOT=/data \
    PATH="/app/.venv/bin:$PATH"

VOLUME /data
EXPOSE 8000

CMD ["python", "-m", "ldp_personal_store.main"]
