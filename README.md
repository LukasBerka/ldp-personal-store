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
uv run python -m app.main
```

With no environment variables set it boots on `http://127.0.0.1:8000/` using every
default, and the views capability (admin-gated `POST /.system/views`) is live
immediately — no seeding step. The admin token is generated and logged once at first
boot (only its SHA-256 hash is persisted), so capture it from the startup log then; it
cannot be recovered later.

Prefer this command over `fastapi run` / bare `uvicorn` for anything reachable off
loopback: it is the only path where uvicorn binds the exact `host`/`port` from the same
`Settings` object that the boot-time TLS precondition validated, so the bind interface
and the TLS check can never drift. `fastapi run` / `uvicorn` take their `--host` from
their own defaults, which can diverge from `LDP_HOST` and defeat the precondition.

For an autoreloading dev server, use the explicit dev-mode alternative:

```sh
uv run fastapi dev app/main.py
```

## Configuration

Every setting has a working default and is read from an `LDP_`-prefixed environment
variable (or a `.env` file). **None are required for a loopback pod.**

| Env var | Default | Purpose |
| --- | --- | --- |
| `LDP_BASE_URI` | `http://localhost:8000/` | Root all pod resource URIs and the `.system`/`.engine` namespaces derive from; normalized to end with `/`. |
| `LDP_STORAGE_ROOT` | `./data` | Filesystem root for pod data (CWD-relative, created on first write). Runtime pod state lives here and is gitignored. |
| `LDP_HOST` | `127.0.0.1` | Bind interface; also drives the TLS precondition below. |
| `LDP_PORT` | `8000` | Bind port. |
| `LDP_TLS_MODE` | `off` | `off` \| `required` \| `terminated` (see below). |
| `LDP_ADMIN_TOKEN` | unset | Plaintext admin token to seed deterministically. Left unset, a random token is generated and logged once at boot. |
| `LDP_RELOAD` | `false` | Dev-only autoreload file-watcher. |

**TLS precondition.** For any non-loopback `host`, `tls_mode` must be `required`
(uvicorn-native TLS) or `terminated` (TLS ended at a trusted reverse proxy upstream),
otherwise boot is refused rather than serving plaintext on a public interface. Note the
current limitation: the `python -m app.main` path does not forward
`ssl_keyfile`/`ssl_certfile`, so uvicorn-native TLS today requires running uvicorn
directly with a matching host, e.g.:

```sh
LDP_HOST=0.0.0.0 LDP_TLS_MODE=required \
  uv run uvicorn app.main:app --host 0.0.0.0 \
  --ssl-keyfile key.pem --ssl-certfile cert.pem
```

## Deployment seam / future split

Both components — storage (LDP + SPARQL) and the view/discovery/stats engine — run in
one process today behind the shared HTTP surface. The engine, discovery, and stats
routers depend exclusively on the `StorageBackend` Protocol (`app/storage/backend.py`)
via `BackendDep` / `app.state.backend`; none of them import the concrete
`FilesystemBackend`, which only `app/main.py` constructs.

A future two-process split therefore needs no change to the engine, discovery, view,
auth, or policy code: supply a new `StorageBackend` implementation (for example an HTTP
client backend talking to a storage process) and swap it in at the single construction
site in the lifespan. The one nontrivial detail for that backend is that
`StorageBackend.query` takes a raw SPARQL string and returns a raw rdflib `Result`, so
an HTTP-backed implementation must serialize the init-bindings and the results across
the wire.

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
