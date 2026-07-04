import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import FastAPI
from pydantic import BaseModel
from rdflib import Graph, URIRef
from rdflib.namespace import RDF

from app import __version__
from app.auth.router import router as system_router
from app.auth.tokens import bootstrap_admin_token
from app.config import check_tls_precondition, get_settings
from app.discovery.router import router as discovery_router
from app.ldp.router import router as ldp_router
from app.sparql.router import router as sparql_router
from app.storage.backend import ResourceNotFound, StorageBackend
from app.storage.filesystem import FilesystemBackend
from app.views.engine import router as engine_router
from app.views.router import router as views_router
from app.vocab import (
    LDP_BasicContainer,
    LDP_RDFSource,
    LDP_Resource,
    make_engine_ns,
    make_system_ns,
)

logger = logging.getLogger(__name__)


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
    plaintext = bootstrap_admin_token(
        backend, app.state.system_ns, admin_token=settings.admin_token
    )
    if plaintext is not None:
        logger.warning("Admin token (capture now, not stored): %s", plaintext)
    yield


app = FastAPI(title="Personal LDP Pod", version=__version__, lifespan=lifespan)


class HealthResponse(BaseModel):
    status: Literal["ok"]
    version: str


@app.get("/health")
def health() -> HealthResponse:
    return HealthResponse(status="ok", version=__version__)


app.include_router(sparql_router)
# The more-specific /.system/views router must precede the /.system catch-all so its
# POST/PUT/DELETE win route resolution before the system router's GET/DELETE
# /{path:path} handlers; both precede the LDP catch-all so admin-gated system paths
# are adjudicated before the public /{path:path} handlers reach a reserved resource.
app.include_router(views_router)
app.include_router(system_router)
# The consumer engine mounts before the LDP catch-all so /.engine/ requests are
# adjudicated here instead of being swallowed by the public /{path:path} handlers.
# The discovery/stats router shares the /.engine prefix and mounts right after the
# engine so its distinct paths (/.engine/discovery, /.engine/stats) are matched
# before the public /{path:path} LDP catch-all; they never collide with
# /.engine/views/{view_id} or /.engine/blob/{view_id}.
app.include_router(engine_router)
app.include_router(discovery_router)
app.include_router(ldp_router)


def run() -> None:
    import uvicorn

    settings = get_settings()
    uvicorn.run("app.main:app", host=settings.host, port=settings.port, reload=settings.reload)


if __name__ == "__main__":
    run()
