"""Engine-role FastAPI app: the view engine as its own process against a remote store.

The engine holds no local storage backend; it reaches an arbitrary standard
LDP + SPARQL 1.1 store over HTTP (``LDP_STORAGE_URL``, authenticated with
``LDP_ENGINE_TOKEN``). The bundled single-process pod lives in ``ldp_pod`` and wires the
same engine to an in-process storage app instead.
"""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from ldp_common.appkit import add_cors, add_health, install_openapi_security, run_uvicorn
from ldp_common.config import check_tls_precondition, get_cors_settings, get_settings
from ldp_common.vocab import make_engine_ns, make_system_ns
from ldp_view_engine import __version__
from ldp_view_engine.client import StorageClient, UpstreamError
from ldp_view_engine.discovery import router as discovery_router
from ldp_view_engine.engine import router as engine_router


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    settings = get_settings()
    check_tls_precondition(settings)
    if settings.storage_url is None:
        raise RuntimeError(
            "the engine role requires LDP_STORAGE_URL (the state store it keeps its records "
            "in); the bundled single-process pod is ldp_pod, not this app."
        )
    engine_token = settings.engine_token
    if engine_token is None:
        raise RuntimeError(
            "the engine role requires LDP_ENGINE_TOKEN — the credential the storage server "
            "seeded for the engine — to authenticate its state-store requests."
        )
    if settings.effective_data_source_url is None:
        raise RuntimeError(
            "the engine role requires a data source: set LDP_DATA_SOURCE_URL, or "
            "LDP_STORAGE_URL to co-locate the data with the state store."
        )
    app.state.system_ns = make_system_ns(settings.base_uri)
    app.state.engine_ns = make_engine_ns(settings.base_uri)
    http = httpx.AsyncClient()
    app.state.storage = StorageClient(
        http,
        base_uri=settings.base_uri,
        state_token=engine_token,
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


_DESCRIPTION = """\
The **view engine**: it shares named, parameterized SPARQL CONSTRUCT views under
revocable consumer bearer tokens with per-grant policies, obtaining the data it serves
from an arbitrary standard LDP + SPARQL 1.1 storage server over HTTP.

Consumers authenticate with a **ConsumerToken** handed over by the pod owner and read only
through this surface (`/.engine/discovery`, `/.engine/views/{id}`, `/.engine/blob/{id}`);
`/.engine/stats` is an owner read authenticated with the **AdminToken**.
"""

_TAGS_METADATA = [
    {"name": "health", "description": "Unauthenticated liveness probe."},
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
    title="Personal LDP Pod — View Engine",
    version=__version__,
    description=_DESCRIPTION,
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


# The consumer engine mounts before discovery so /.engine/views and /.engine/blob resolve
# ahead of the distinct /.engine/discovery and /.engine/stats paths.
app.include_router(engine_router)
app.include_router(discovery_router)


def run() -> None:
    run_uvicorn("ldp_view_engine.app:app", get_settings())


if __name__ == "__main__":
    run()
