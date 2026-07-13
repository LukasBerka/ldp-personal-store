"""Storage-role FastAPI app: the reference LDP + SPARQL 1.1 server and the pod owner's
administration surface, with no view engine mounted.

This is what a split deployment runs as the data source / state store; the view engine
runs as its own process (``ldp_view_engine``) and reaches this server over HTTP. The
bundled single-process pod is ``ldp_personal_store.main``, which composes both roles.
"""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from ldp_common.appkit import add_cors, add_health, install_openapi_security, run_uvicorn
from ldp_common.config import (
    check_tls_precondition,
    get_cors_settings,
    get_settings,
    require_admin_token,
)
from ldp_common.vocabulary import make_engine_ns, make_system_ns
from ldp_personal_store import __version__
from ldp_personal_store.authentication.router import router as system_router
from ldp_personal_store.authentication.tokens_store import (
    bootstrap_admin_token,
    bootstrap_engine_token,
)
from ldp_personal_store.bootstrap import init_root_container
from ldp_personal_store.ldp.router import add_constraints_route
from ldp_personal_store.ldp.router import router as ldp_router
from ldp_personal_store.sparql.router import router as sparql_router
from ldp_personal_store.storage.filesystem import FilesystemBackend
from ldp_personal_store.storage.router import router as storage_internal_router
from ldp_personal_store.views.router import router as views_router


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    settings = get_settings()
    check_tls_precondition(settings)
    app.state.system_ns = make_system_ns(settings.base_uri)
    app.state.engine_ns = make_engine_ns(settings.base_uri)
    backend = FilesystemBackend(storage_root=settings.storage_root, base_uri=settings.base_uri)
    app.state.backend = backend
    init_root_container(backend, settings.base_uri)
    bootstrap_admin_token(backend, app.state.system_ns, admin_token=require_admin_token(settings))
    # Seed the engine's storage credential so a separately-deployed engine can
    # authenticate against this server; the plaintext is set out of band via
    # LDP_ENGINE_TOKEN in a split deployment.
    bootstrap_engine_token(backend, app.state.system_ns, engine_token=settings.engine_token)
    yield


_DESCRIPTION = """\
The **storage role** of the LDP Personal Store: RDF and binary storage behind a uniform
LDP/HTTP surface, a read-only SPARQL 1.1 Protocol endpoint, and the pod owner's `.system/`
administration tree (views, grants, policies, the access log). This is a standard
LDP + SPARQL 1.1 server; the view engine is a separate product that reads through it.

Two bearer credentials operate here: the **AdminToken** (owner; the full surface) and the
**EngineToken** (a separately-deployed view engine's storage credential: reads plus the
standard-LDP enforcement writes). Consumers never talk to this server directly — they hold
a ConsumerToken and read through the engine.
"""

_TAGS_METADATA = [
    {"name": "health", "description": "Unauthenticated liveness probe."},
    {
        "name": "ldp",
        "description": (
            "The pod's data plane: LDP resources, containers, and binaries at arbitrary "
            "paths. Reads accept the admin or engine token; writes are owner-only. The "
            "`.system/` prefix is reserved and handled by its own endpoint groups."
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
            "with typed parameters."
        ),
    },
    {
        "name": "system",
        "description": (
            "The reserved `.system/` management tree: issuing grants, authoring policies, "
            "browsing views, tokens, policies, and the access log as LDP containers, and "
            "revoking records."
        ),
    },
    {
        "name": "system-internal",
        "description": (
            "Enforcement writes used by the view engine after each delivery. Documented "
            "for completeness and split deployments; a frontend client never calls these."
        ),
    },
]

app = FastAPI(
    title="LDP Personal Store — Storage",
    version=__version__,
    description=_DESCRIPTION,
    openapi_tags=_TAGS_METADATA,
    lifespan=lifespan,
)

add_cors(app, get_cors_settings())
install_openapi_security(app)
add_health(app, __version__)
add_constraints_route(app)

# The more-specific /.system/views router must precede the /.system catch-all, and the
# engine's state-write surface likewise; all precede the LDP /{path:path} catch-all so
# reserved system paths are adjudicated before the generic data-plane handler.
app.include_router(sparql_router)
app.include_router(views_router)
app.include_router(storage_internal_router)
app.include_router(system_router)
app.include_router(ldp_router)


def run() -> None:
    run_uvicorn("ldp_personal_store.app:app", get_settings())


if __name__ == "__main__":
    run()
