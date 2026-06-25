"""LDP HTTP layer: RDF resource and container endpoints over the storage backend.

Handlers are synchronous: the backend performs blocking rdflib, lock, and
filesystem work, and FastAPI runs sync path operations in a threadpool, which is
the correct execution model for blocking code.
"""

from typing import Annotated

from fastapi import APIRouter, Header, HTTPException, Response

from app.config import SettingsDep
from app.ldp.content import (
    ALLOW_RDF,
    etag_for_graph,
    link_header,
    negotiate,
    serialize_graph,
)
from app.ldp.deps import BackendDep
from app.storage.backend import (
    NotABinaryResource,
    PrefixViolation,
    ResourceNotFound,
    StorageBackend,
    StorageError,
)
from app.vocab import LDP_RDFSource, LDP_Resource

router = APIRouter(tags=["ldp"])


def _http_error(exc: StorageError) -> HTTPException:
    """Translate a storage-layer exception into its HTTP equivalent."""
    if isinstance(exc, ResourceNotFound):
        return HTTPException(status_code=404)
    if isinstance(exc, PrefixViolation):
        return HTTPException(status_code=403)
    if isinstance(exc, NotABinaryResource):
        return HTTPException(status_code=409)
    return HTTPException(status_code=500)


def _get_rdf_resource(
    backend: StorageBackend,
    base_uri: str,
    path: str,
    accept: str | None,
    if_none_match: str | None,
) -> Response:
    uri = base_uri + path
    try:
        graph = backend.read(uri)
    except StorageError as exc:
        raise _http_error(exc) from exc
    fmt, media_type = negotiate(accept)
    etag = etag_for_graph(graph)
    if if_none_match is not None and if_none_match in (etag, "*"):
        return Response(status_code=304, headers={"ETag": etag})
    return Response(
        content=serialize_graph(graph, fmt),
        media_type=media_type,
        headers={
            "ETag": etag,
            "Link": link_header([LDP_Resource, LDP_RDFSource]),
            "Allow": ALLOW_RDF,
        },
    )


@router.api_route("/", methods=["GET", "HEAD"])
def get_root(
    backend: BackendDep,
    settings: SettingsDep,
    accept: Annotated[str | None, Header()] = None,
    if_none_match: Annotated[str | None, Header()] = None,
) -> Response:
    return _get_rdf_resource(backend, settings.base_uri, "", accept, if_none_match)


@router.api_route("/{path:path}", methods=["GET", "HEAD"])
def get_resource(
    path: str,
    backend: BackendDep,
    settings: SettingsDep,
    accept: Annotated[str | None, Header()] = None,
    if_none_match: Annotated[str | None, Header()] = None,
) -> Response:
    return _get_rdf_resource(backend, settings.base_uri, path, accept, if_none_match)


@router.options("/")
def options_root() -> Response:
    return Response(status_code=200, headers={"Allow": ALLOW_RDF})


@router.options("/{path:path}")
def options_resource() -> Response:
    return Response(status_code=200, headers={"Allow": ALLOW_RDF})
