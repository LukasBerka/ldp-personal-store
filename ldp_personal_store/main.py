"""The bundled Personal LDP Pod: the view engine and reference storage in one process.

This composition root mounts both surfaces' routers on a single FastAPI app and wires the
engine's storage client to that same app over an in-process ASGI transport — the identical
HTTP surface a split deployment reaches over the network. The view-engine product stays
independent (``ldp_view_engine`` never imports ``ldp_personal_store``); this module is the
one place that depends on both. The canonical zero-config run command is
``LDP_ADMIN_TOKEN=… uv run python -m ldp_personal_store.main``.
"""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from ldp_common.appkit import add_cors, add_health, install_openapi_security, run_uvicorn
from ldp_common.config import (
    check_tls_precondition,
    get_cors_settings,
    get_settings,
    require_admin_token,
)
from ldp_common.vocab import make_engine_ns, make_system_ns
from ldp_personal_store import __version__
from ldp_personal_store.auth.router import router as system_router
from ldp_personal_store.auth.tokens_store import bootstrap_admin_token, bootstrap_engine_token
from ldp_personal_store.bootstrap import init_root_container
from ldp_personal_store.ldp.router import router as ldp_router
from ldp_personal_store.sparql.router import router as sparql_router
from ldp_personal_store.storage.filesystem import FilesystemBackend
from ldp_personal_store.storage.router import router as storage_internal_router
from ldp_personal_store.views.router import router as views_router
from ldp_view_engine.client import StorageClient, UpstreamError
from ldp_view_engine.discovery import router as discovery_router
from ldp_view_engine.engine import router as engine_router


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    settings = get_settings()
    check_tls_precondition(settings)
    # The bundled pod reaches storage in-process (ASGI), which routes every request to
    # this app regardless of its URL. A distinct data source is therefore unreachable from
    # the bundled process — refuse rather than silently serve view data from ourselves.
    if settings.storage_url is None and settings.data_source_url is not None:
        raise RuntimeError(
            "the bundled pod reaches storage in-process and cannot target a separate "
            "LDP_DATA_SOURCE_URL; run the engine as its own process (python -m "
            "ldp_view_engine.app) for a separate data source, or unset LDP_DATA_SOURCE_URL."
        )
    app.state.system_ns = make_system_ns(settings.base_uri)
    app.state.engine_ns = make_engine_ns(settings.base_uri)
    backend = FilesystemBackend(storage_root=settings.storage_root, base_uri=settings.base_uri)
    app.state.backend = backend
    init_root_container(backend, settings.base_uri)
    bootstrap_admin_token(backend, app.state.system_ns, admin_token=require_admin_token(settings))

    # The engine's storage credential and HTTP client: the bundled deployment talks
    # to this same app over an in-process ASGI transport — the identical HTTP
    # surface a split deployment reaches over the network via LDP_STORAGE_URL.
    engine_plaintext = bootstrap_engine_token(
        backend, app.state.system_ns, engine_token=settings.engine_token
    )
    if settings.storage_url is not None:
        http = httpx.AsyncClient()
    else:
        http = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
            base_url=settings.base_uri,
        )
    app.state.storage = StorageClient(
        http,
        base_uri=settings.base_uri,
        state_token=engine_plaintext,
        state_url=settings.storage_url,
        data_url=settings.effective_data_source_url,
        data_base_uri=settings.effective_data_source_base_uri,
        data_token=settings.effective_data_source_token,
        data_auth=settings.data_source_auth,
        state_graph=settings.state_graph,
    )
    try:
        yield
    finally:
        await http.aclose()


_API_DESCRIPTION = """\
A self-hostable personal Linked Data Platform (LDP) pod: RDF and binary storage behind a
uniform LDP/HTTP surface, a read-only SPARQL 1.1 Protocol endpoint, and a view engine that
shares named, parameterized SPARQL CONSTRUCT views under revocable bearer tokens with
per-grant policies. This description plus the per-operation documentation is intended to be
sufficient, on its own, to build a complete client.

## Credentials

Three bearer tokens exist (see *Authorize* / `securitySchemes` for details):

| Credential | Held by | Surface |
|---|---|---|
| **AdminToken** | pod owner | everything: data, `/.system/*`, `/sparql`, `/.engine/stats` |
| **EngineToken** | the view engine itself | internal storage reads + enforcement writes |
| **ConsumerToken** | a data consumer | `/.engine/` discovery, views, and blob endpoints |

A frontend for the **pod owner** authenticates every request with the admin token.
A frontend for a **consumer** needs only the consumer token the owner handed over.

## Data model

Everything the owner manages — data resources, containers, view definitions, grants,
policies, the access log — is an RDF resource. RDF request and response bodies are
exchanged in four serializations, negotiated via `Content-Type` / `Accept`:
`text/turtle` (default), `application/ld+json`, `application/n-triples`,
`application/rdf+xml`. Any other `Content-Type` on a write stores the body as an opaque
binary. The system vocabulary uses the prefix `pod:` = `urn:pod:vocab:`;
`dcterms:` = `http://purl.org/dc/terms/`; `ldp:` = `http://www.w3.org/ns/ldp#`.

## Owner workflow

1. `PUT` / `POST` data anywhere outside the reserved `.system/` and `.engine/` prefixes.
2. `POST /.system/views` — define a view: a SPARQL CONSTRUCT template plus typed parameters.
3. `POST /.system/tokens` — issue a grant naming the views it unlocks; capture the one-time
   `pod:tokenSecret` from the response and hand it to the consumer out of band.
4. `PUT /.system/tokens/policies/{id}` — optionally bound the grant (expiry, validity
   window, retrieval count, rate).
5. `GET /.engine/stats` audits deliveries; `DELETE /.system/tokens/{id}` revokes a grant
   instantly.

## Consumer workflow

1. `GET /.engine/discovery` — list the views this token unlocks, with their parameter shapes.
2. `GET /.engine/views/{id}?param=value…` — fetch a view's result.
3. Results reference other shared resources (including binaries) only through
   `/.engine/blob/{id}?uri=…` proxy URLs; dereference them with the same token, unchanged.

## Error contract

* Every authentication failure is an identical `401` — the response never reveals whether
  a token exists, is revoked, or is of the wrong kind.
* `403` on the consumer surface means the view is outside the grant's scope or a policy
  constraint denied the request (the JSON `detail` names the violated constraint).
* `400` unparsable RDF or SPARQL, `409` deleting a non-empty container, `412` failed
  `If-Match`/`If-None-Match` precondition, `415` unsupported media type, `422` invalid
  view/policy/parameter shape (the `detail` explains), `502` the engine could not reach
  storage (e.g. its credential was revoked).
"""

_TAGS_METADATA = [
    {"name": "health", "description": "Unauthenticated liveness probe."},
    {
        "name": "ldp",
        "description": (
            "The pod's data plane: LDP resources, containers, and binaries at arbitrary "
            "paths. Reads accept the admin or engine token; writes are owner-only. The "
            "`.system/` and `.engine/` prefixes are reserved and handled by their own "
            "endpoint groups."
        ),
    },
    {
        "name": "sparql",
        "description": (
            "Read-only SPARQL 1.1 Protocol endpoint over the pod's full RDF data, for the "
            "owner and the engine (consumers query only through views)."
        ),
    },
    {
        "name": "views",
        "description": (
            "Owner-side management of view definitions: named SPARQL CONSTRUCT templates "
            "with typed parameters. Reading a definition or listing the catalog is a plain "
            "`GET` on `/.system/views/…` (see the *system* group)."
        ),
    },
    {
        "name": "system",
        "description": (
            "The reserved `.system/` management tree: issuing grants, authoring policies, "
            "browsing views, tokens, policies, and the access log as LDP containers, and "
            "revoking records. RDF in and out, like the rest of the pod."
        ),
    },
    {
        "name": "system-internal",
        "description": (
            "Enforcement writes used by the view engine after each delivery. Documented "
            "for completeness and split deployments; a frontend client never calls these."
        ),
    },
    {
        "name": "engine",
        "description": (
            "The consumer surface: fetch a view's result and dereference the gated proxy "
            "URLs found inside it. Authenticate with the consumer token from the pod owner."
        ),
    },
    {
        "name": "discovery",
        "description": (
            "Consumer discovery and owner statistics: what a grant unlocks, and how often "
            "each view was delivered to whom."
        ),
    },
]

app = FastAPI(
    title="Personal LDP Pod",
    version=__version__,
    description=_API_DESCRIPTION,
    openapi_tags=_TAGS_METADATA,
    lifespan=lifespan,
)

add_cors(app, get_cors_settings())
install_openapi_security(app)
add_health(app, __version__)


@app.exception_handler(UpstreamError)
def upstream_error_handler(request: Request, exc: UpstreamError) -> JSONResponse:
    # The engine could not complete a storage-boundary call — most prominently when
    # the pod owner has revoked the engine's token. 502: the gateway's upstream refused.
    return JSONResponse(status_code=502, content={"detail": "storage upstream refused"})


# Routers mount in a deliberate order: the specific /.system/views router must precede the
# /.system catch-all, the engine's state-write surface likewise, and the consumer engine and
# discovery routers before the LDP /{path:path} catch-all so reserved .system/ and .engine/
# paths are adjudicated before the generic data-plane handler.
app.include_router(sparql_router)
app.include_router(views_router)
app.include_router(storage_internal_router)
app.include_router(system_router)
app.include_router(engine_router)
app.include_router(discovery_router)
app.include_router(ldp_router)


def run() -> None:
    run_uvicorn("ldp_personal_store.main:app", get_settings())


if __name__ == "__main__":
    run()
