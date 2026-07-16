# LDP Personal Store

A self-hostable **LDP Personal Store**: a FastAPI [Linked Data Platform](https://www.w3.org/TR/ldp/)
server that stores your RDF and binary data behind a uniform LDP/HTTP surface with a
read-only SPARQL 1.1 endpoint, and shares precise, query-defined slices of it with chosen
consumers under revocable, policy-bounded bearer tokens — using only standard LDP/HTTP
clients, no proprietary client required.

Managed with [uv](https://docs.astral.sh/uv/), type-checked with [pyrefly](https://pyrefly.org/)
and linted/formatted with [ruff](https://docs.astral.sh/ruff/).

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

## Try it — walk the use case scenarios

The **[`demo/`](demo/README.md)** directory shows the pod doing what it is
built for, as copy-paste walkthroughs of its use case scenarios with the
expected responses shown: build a **calendar share by hand** — insert events,
author a SPARQL
`CONSTRUCT` view, issue a policy-bounded grant, consume it, revoke it — then a
**photo album** streaming real PNGs through gated links, a **lecture-notes
hierarchy** shared one course folder at a time, a **shopping list** that stays
shared while its data churns, and a **reading list** whose review window the
owner closes live. One script seeds all the data, views, grants, and policies:

```sh
# both terminals at the repository root
LDP_ADMIN_TOKEN=devtoken uv run python -m ldp_personal_store.main   # terminal 1
ADMIN=devtoken ./test_data/seed.sh                                  # terminal 2
```

Start with [`demo/README.md`](demo/README.md) — a five-minute tour, then one
page per scenario, played as both the owner (plain `curl`) and the consumer
(`curl` or the browser [test console](testing_client/README.md)).

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

### Split deployment

The compose file also defines a **`split` profile** that runs the view engine and the
storage server as separate containers behind a reverse proxy (Caddy), communicating only
over the standard LDP + SPARQL 1.1 contract — the same topology a production deployment
would use to place the engine in front of a third-party store:

```sh
LDP_ADMIN_TOKEN=<secret> LDP_ENGINE_TOKEN=<secret> \
  docker compose --profile split up --build
```

Here `LDP_ENGINE_TOKEN` is the credential the engine presents to storage; the compose file
seeds it on both containers, so it must be provided. The proxy publishes one canonical base
(`LDP_BASE_URI`) and routes the consumer surface (`/.engine/*`) to the engine and everything
else to storage, so the record URIs storage mints and the proxy URLs the engine emits share
one base. See the comments in `docker-compose.yml` and the "Split-deployment settings" table
under [Configuration](#configuration) for the full set of knobs.

## Clients and test harnesses

Three companion tools live alongside the server. None is part of the shipped pod; each
drives it over the same standard HTTP surface and is excluded from the packaged app.

- **`testing_client/`** — a zero-dependency static **test console** (the current one). A
  single-page web app (open `index.html`, no build step, no server) that drives the pod's
  HTTP API as both **owner** — write data, define parameterized SPARQL `CONSTRUCT` views,
  issue policy-bounded grants, run SPARQL, read delivery stats — and **consumer** — discover
  the views a grant unlocks and read their results through the engine surface. See
  `testing_client/README.md`.

- **`penny/`** — a shim that lets [**Penny**](https://penny.vincenttunru.com/), a real
  third-party Solid/LDP data browser, drive the pod. An auth-injecting reverse proxy
  (`proxy.py`) stamps the bearer token onto Penny's credential-less requests and normalises
  CORS, so a GUI that expects Solid-OIDC login browses the pod's static-token surface with
  no login. `./run.sh` starts pod + proxy for owner or consumer browsing. See
  `penny/README.md`.

- **`w3c_ldp_test_suite/`** — the official [W3C LDP Test Suite](https://github.com/w3c/ldp-testsuite)
  wired to run against the pod, producing [EARL](https://www.w3.org/TR/EARL10-Schema/)
  conformance reports. Everything runs in Docker containers on one network (`run.sh`), with
  a dependency-free auth proxy (`auth_proxy.py`) that adds the bearer token the suite cannot
  send itself, so the server runs unchanged under test. See `w3c_ldp_test_suite/README.md`.

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

**Split-deployment settings.** By default the view engine and storage server run in one
process, reaching each other over an in-process ASGI transport — the same HTTP surface a
split deployment would reach over the network. The settings below only take effect when the
engine and storage run as **separate processes** (see "Run with Docker" for the compose
profile that does this); the bundled single-process pod leaves them all at their defaults.

| Env var | Default | Purpose |
| --- | --- | --- |
| `LDP_ENGINE_TOKEN` | fresh per boot | Plaintext credential the view engine presents to storage on the engine→storage boundary; only its SHA-256 hash is persisted. Unset (bundled), a new token is minted each startup and kept in process memory; set the **same** value on the engine and the storage server when they run as separate processes. |
| `LDP_STATE_STORAGE_URL` | in-process | Base URL of the upstream storage server holding the engine's state records. Unset (bundled), the engine reaches storage over an in-process ASGI transport (same surface, no socket); set it to run the engine against a storage server listening elsewhere (loopback or remote). |
| `LDP_STATE_GRAPH` | `urn:ldp:engine-state` | Named graph holding the engine's operating state (token/view/policy records and the access log), kept out of view-CONSTRUCT scope. A stable logical name the engine and store agree on; this server realizes it as its reserved `.system/` subtree. |
| `LDP_DATA_SOURCE_URL` | `LDP_STATE_STORAGE_URL` | The store the engine queries for view CONSTRUCTs and binary reads, as opposed to the state store above. Unset, it is co-located with the state store; set it to point the engine at a separate SPARQL/LDP data source (e.g. a third-party store). |
| `LDP_DATA_SOURCE_BASE_URI` | `LDP_BASE_URI` | The namespace data-source resources carry — the "upstream" URIs the engine rewrites into gated proxy URLs and guards the blob endpoint against. Unset, it matches the engine's own public base. |
| `LDP_DATA_SOURCE_TOKEN` | `LDP_ENGINE_TOKEN` | Credential the engine presents to the data source. Unset, it reuses the engine token (co-located: same credential as the state store). |
| `LDP_DATA_SOURCE_AUTH` | `bearer` | Auth scheme the engine uses against the data source: `bearer` \| `basic` (`LDP_DATA_SOURCE_TOKEN` as `user:password`) \| `none`. |

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
