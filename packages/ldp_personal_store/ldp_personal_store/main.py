from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Literal

import httpx
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from rdflib import Graph, URIRef
from rdflib.namespace import RDF

from ldp_common.apidocs import SECURITY_SCHEMES
from ldp_common.config import check_tls_precondition, get_cors_settings, get_settings
from ldp_common.vocab import (
    LDP_BasicContainer,
    LDP_RDFSource,
    LDP_Resource,
    make_engine_ns,
    make_system_ns,
)
from ldp_personal_store import __version__
from ldp_personal_store.auth.router import router as system_router
from ldp_personal_store.auth.tokens_store import bootstrap_admin_token, bootstrap_engine_token
from ldp_personal_store.discovery.router import router as discovery_router
from ldp_personal_store.ldp.router import router as ldp_router
from ldp_personal_store.sparql.router import router as sparql_router
from ldp_personal_store.storage.backend import ResourceNotFound, StorageBackend
from ldp_personal_store.storage.filesystem import FilesystemBackend
from ldp_personal_store.storage.router import router as storage_internal_router
from ldp_personal_store.upstream import StorageClient, UpstreamError
from ldp_personal_store.views.engine import router as engine_router
from ldp_personal_store.views.router import router as views_router


def _init_root_container(backend: StorageBackend, base_uri: str) -> None:
    """Seed the pod root as an empty Basic Container on first startup.

    The root URI is not under the reserved ``.system/`` subtree, so the public
    write path accepts it.
    """
    try:
        backend.read(base_uri)
    except ResourceNotFound:
        root = URIRef(base_uri)
        graph = Graph()
        graph.add((root, RDF.type, LDP_Resource))
        graph.add((root, RDF.type, LDP_RDFSource))
        graph.add((root, RDF.type, LDP_BasicContainer))
        backend.write(base_uri, graph)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    settings = get_settings()
    check_tls_precondition(settings)
    app.state.system_ns = make_system_ns(settings.base_uri)
    app.state.engine_ns = make_engine_ns(settings.base_uri)
    backend = FilesystemBackend(storage_root=settings.storage_root, base_uri=settings.base_uri)
    app.state.backend = backend
    _init_root_container(backend, settings.base_uri)
    bootstrap_admin_token(backend, app.state.system_ns, admin_token=settings.admin_token)

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
        http=http,
        token=engine_plaintext,
        base_uri=settings.base_uri,
        storage_url=settings.storage_url,
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

app.add_middleware(
    CORSMiddleware,
    allow_origins=get_cors_settings().allow_origins,
    allow_credentials=False,
    allow_methods=["GET", "HEAD", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=[
        "Authorization",
        "Content-Type",
        "Accept",
        "If-Match",
        "If-None-Match",
        "Prefer",
        "Slug",
    ],
    expose_headers=[
        "ETag",
        "Location",
        "Link",
        "Allow",
        "Accept-Post",
        "Preference-Applied",
        "WWW-Authenticate",
    ],
    max_age=600,
)


def _openapi_with_security() -> dict:
    """The generated schema plus the bearer security schemes.

    Routes reference the schemes by name through ``openapi_extra``; the scheme
    definitions themselves have no FastAPI dependency to hang off (token
    validation is a plain header check), so they are attached here.
    """
    if app.openapi_schema:
        return app.openapi_schema
    schema = get_openapi(
        title=app.title,
        version=app.version,
        description=app.description,
        routes=app.routes,
        tags=app.openapi_tags,
    )
    schema.setdefault("components", {})["securitySchemes"] = SECURITY_SCHEMES
    # Raw-bytes body parameters auto-generate an application/json binary placeholder
    # that survives the openapi_extra merge; drop it wherever a route documented its
    # real media types.
    for path_item in schema["paths"].values():
        for operation in path_item.values():
            content = operation.get("requestBody", {}).get("content", {})
            json_schema = content.get("application/json", {}).get("schema", {})
            is_bytes_placeholder = (
                json_schema.get("format") == "binary"
                or json_schema.get("contentMediaType") == "application/octet-stream"
            )
            if len(content) > 1 and is_bytes_placeholder:
                del content["application/json"]
    app.openapi_schema = schema
    return schema


app.openapi = _openapi_with_security  # type: ignore[method-assign]


@app.exception_handler(UpstreamError)
def upstream_error_handler(request: Request, exc: UpstreamError) -> JSONResponse:
    # The engine could not complete a storage-boundary call — most prominently when
    # the pod owner has revoked the engine's token. 502: the gateway's upstream refused.
    return JSONResponse(status_code=502, content={"detail": "storage upstream refused"})


class HealthResponse(BaseModel):
    status: Literal["ok"]
    version: str


@app.get(
    "/health",
    tags=["health"],
    operation_id="healthCheck",
    summary="Liveness probe",
    description="Unauthenticated readiness/liveness check reporting the server version.",
)
def health() -> HealthResponse:
    return HealthResponse(status="ok", version=__version__)


app.include_router(sparql_router)
# The more-specific /.system/views router must precede the /.system catch-all so its
# POST/PUT/DELETE win route resolution before the system router's GET/DELETE
# /{path:path} handlers; the engine's state-write surface (token PUT, access-log POST)
# likewise mounts before the system router. All precede the LDP catch-all so admin-gated
# system paths are adjudicated before the /{path:path} handlers reach a reserved resource.
app.include_router(views_router)
app.include_router(storage_internal_router)
app.include_router(system_router)
# The consumer engine mounts before the LDP catch-all so /.engine/ requests are
# adjudicated here instead of being swallowed by the /{path:path} handlers.
# The discovery/stats router shares the /.engine prefix and mounts right after the
# engine so its distinct paths (/.engine/discovery, /.engine/stats) are matched
# before the LDP catch-all; they never collide with /.engine/views/{view_id} or
# /.engine/blob/{view_id}.
app.include_router(engine_router)
app.include_router(discovery_router)
app.include_router(ldp_router)


def run() -> None:
    import uvicorn

    settings = get_settings()
    # Enforced here rather than in check_tls_precondition: a direct uvicorn launch
    # supplies certs as CLI flags the Settings object never sees, so only this
    # launch path can know the files are actually being handed to uvicorn.
    if settings.tls_mode == "required" and (
        settings.ssl_keyfile is None or settings.ssl_certfile is None
    ):
        raise RuntimeError(
            "tls_mode='required' needs LDP_SSL_KEYFILE and LDP_SSL_CERTFILE so uvicorn "
            "can terminate TLS; set both, or use tls_mode='terminated' behind a "
            "TLS-terminating reverse proxy."
        )
    uvicorn.run(
        "ldp_personal_store.main:app",
        host=settings.host,
        port=settings.port,
        reload=settings.reload,
        ssl_keyfile=settings.ssl_keyfile,
        ssl_certfile=settings.ssl_certfile,
    )


if __name__ == "__main__":
    run()
