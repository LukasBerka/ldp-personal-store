# bachelor-thesis-project

A FastAPI application managed with [uv](https://docs.astral.sh/uv/), type-checked
with [pyrefly](https://pyrefly.org/) and linted/formatted with [ruff](https://docs.astral.sh/ruff/).

## Prerequisites

Install uv (see the [uv install docs](https://docs.astral.sh/uv/getting-started/installation/)):

```sh
pip install uv
```

## Setup

Install all dependencies (creates `.venv` and resolves from `uv.lock`):

```sh
uv sync
```

`uv run <cmd>` automatically uses this environment, so the virtual environment
never has to be activated manually.

## Run the app

The recommended command starts a working pod with **zero configuration** required:

```sh
LDP_ADMIN_TOKEN=<owner-chosen-secret> uv run python -m ldp_personal_store.main
```

`LDP_ADMIN_TOKEN` is the one required setting — the pod refuses to start without it, and
never writes the plaintext to the log (only its SHA-256 hash is persisted). With it set,
the pod boots on `http://127.0.0.1:8000/` using every other default, and the views
capability (admin-gated `POST /.system/views`) is live immediately — no seeding step.
Rotating the credential is a restart with a new `LDP_ADMIN_TOKEN`; the admin record is
reconciled to it on the next boot.

Prefer this command over `fastapi run` / bare `uvicorn` for anything reachable off
loopback: it is the only path where uvicorn binds the exact `host`/`port` from the same
`Settings` object that the boot-time TLS precondition validated, so the bind interface
and the TLS check can never drift. `fastapi run` / `uvicorn` take their `--host` from
their own defaults, which can diverge from `LDP_HOST` and defeat the precondition.

For an autoreloading dev server, use the explicit dev-mode alternative:

```sh
uv run fastapi dev ldp_personal_store/main.py
```

## Run with Docker

The repository ships a `Dockerfile` and `docker-compose.yml` that package the pod as a
container:

```sh
docker compose up --build
```

The image binds `0.0.0.0` (a container is never reached over its own loopback) with
`LDP_TLS_MODE=terminated`, and the compose file publishes the port on the host loopback
only (`127.0.0.1:8000`), so plaintext never reaches a public interface; a public
deployment puts a TLS-terminating reverse proxy in front of the container instead. Pod
state persists in the named volume `pod-data` mounted at `/data`. `LDP_ADMIN_TOKEN` must
be provided through the compose environment (host environment or an `.env` file); the
container refuses to start without it and never writes the plaintext to the log.

## Configuration

Every setting except the admin token has a working default and is read from an
`LDP_`-prefixed environment variable (or a `.env` file). **Only `LDP_ADMIN_TOKEN` is
required — the pod refuses to start without it.**

| Env var | Default | Purpose |
| --- | --- | --- |
| `LDP_BASE_URI` | `http://localhost:8000/` | Root all pod resource URIs and the `.system`/`.engine` namespaces derive from; normalized to end with `/`. |
| `LDP_STORAGE_ROOT` | `./data` | Filesystem root for pod data (CWD-relative, created on first write). Runtime pod state lives here and is gitignored. |
| `LDP_HOST` | `127.0.0.1` | Bind interface; also drives the TLS precondition below. |
| `LDP_PORT` | `8000` | Bind port. |
| `LDP_TLS_MODE` | `off` | `off` \| `required` \| `terminated` (see below). |
| `LDP_SSL_KEYFILE` | unset | TLS private key for `tls_mode=required`; forwarded to uvicorn by `python -m ldp_personal_store.main`. |
| `LDP_SSL_CERTFILE` | unset | TLS certificate for `tls_mode=required`; forwarded to uvicorn by `python -m ldp_personal_store.main`. |
| `LDP_ADMIN_TOKEN` | **required** | The pod owner's plaintext admin credential. The pod refuses to start without it; only its SHA-256 hash is persisted and the plaintext is never logged. Choose a long random value (e.g. `openssl rand -base64 32`). |
| `LDP_RELOAD` | `false` | Dev-only autoreload file-watcher. |
| `LDP_CORS_ALLOW_ORIGINS` | `*` | Comma-separated browser origins allowed to read the pod cross-origin (CORS), or `*` for any. `*` is a safe default here because auth is a bearer token in the `Authorization` header, never a cookie — see below. |

**TLS precondition.** For any non-loopback `host`, `tls_mode` must be `required`
(uvicorn-native TLS) or `terminated` (TLS ended at a trusted reverse proxy upstream),
otherwise boot is refused rather than serving plaintext on a public interface. With
`tls_mode=required`, the `python -m ldp_personal_store.main` path hands `LDP_SSL_KEYFILE` /
`LDP_SSL_CERTFILE` to uvicorn and refuses to start when either is missing:

```sh
LDP_HOST=0.0.0.0 LDP_TLS_MODE=required \
  LDP_SSL_KEYFILE=key.pem LDP_SSL_CERTFILE=cert.pem \
  uv run python -m ldp_personal_store.main
```

A direct uvicorn launch may pass `--ssl-keyfile`/`--ssl-certfile` instead; in that
case the launch flags, not the pod, are what guarantee TLS actually terminates.

**CORS.** A consumer's app is typically a browser SPA served from a different origin
than the pod, and every consumer request carries an `Authorization` bearer header — a
non-safelisted header that forces a CORS preflight. The pod answers that preflight and
exposes the LDP response headers a browser needs (`ETag`, `Location`, `Link`, `Allow`,
`Accept-Post`, `Preference-Applied`, `WWW-Authenticate`). Because auth is always an
explicit bearer token in a header — never a cookie or other ambient credential — the pod
never sets `Access-Control-Allow-Credentials`, and so `LDP_CORS_ALLOW_ORIGINS=*` is a
safe default: a hostile page still cannot read a view without the consumer token. Set
`LDP_CORS_ALLOW_ORIGINS` to a comma-separated origin list to restrict which sites may
call the pod from a browser (requests from other clients — curl, rdflib — are unaffected,
as CORS is a browser-enforced policy).

## Lint and format (ruff)

```sh
uv run ruff check .
uv run ruff check --fix .
uv run ruff format .
uv run ruff format --check .
```

## Type-check (pyrefly)

```sh
uv run pyrefly check
```

## Tests (pytest)

```sh
uv run pytest
```
