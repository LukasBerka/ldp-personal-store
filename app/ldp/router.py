"""LDP HTTP layer: RDF resource and container endpoints over the storage backend.

Handlers are synchronous: the backend performs blocking rdflib, lock, and
filesystem work, and FastAPI runs sync path operations in a threadpool, which is
the correct execution model for blocking code.
"""

from typing import Annotated

from fastapi import APIRouter, Body, Header, HTTPException, Response
from rdflib import Graph

from app.config import SettingsDep
from app.ldp.content import (
    ALLOW_RDF,
    RDF_CONTENT_TYPES,
    check_preconditions,
    etag_for_graph,
    link_header,
    negotiate,
    rdflib_format_for,
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

_RDF_LINK = link_header([LDP_Resource, LDP_RDFSource])


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
        headers={"ETag": etag, "Link": _RDF_LINK, "Allow": ALLOW_RDF},
    )


def _put_rdf_resource(
    backend: StorageBackend,
    base_uri: str,
    path: str,
    body: bytes,
    content_type: str | None,
    if_match: str | None,
    if_none_match: str | None,
) -> Response:
    uri = base_uri + path
    normalized_ct = (content_type or "text/turtle").split(";")[0].strip().lower()
    if normalized_ct not in RDF_CONTENT_TYPES:
        raise HTTPException(status_code=415, detail=f"Unsupported media type {normalized_ct!r}")
    try:
        current_etag: str | None = etag_for_graph(backend.read(uri))
        exists = True
    except ResourceNotFound:
        current_etag = None
        exists = False
    except StorageError as exc:
        raise _http_error(exc) from exc
    check_preconditions(if_match, if_none_match, current_etag, exists)
    graph = Graph()
    graph.parse(data=body, format=rdflib_format_for(normalized_ct))
    try:
        backend.write(uri, graph)
    except StorageError as exc:
        raise _http_error(exc) from exc
    return Response(
        status_code=200 if exists else 201,
        headers={
            "ETag": etag_for_graph(graph),
            "Location": uri,
            "Link": _RDF_LINK,
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


@router.put("/")
def put_root(
    backend: BackendDep,
    settings: SettingsDep,
    body: Annotated[bytes, Body()],
    content_type: Annotated[str | None, Header()] = None,
    if_match: Annotated[str | None, Header()] = None,
    if_none_match: Annotated[str | None, Header()] = None,
) -> Response:
    return _put_rdf_resource(
        backend, settings.base_uri, "", body, content_type, if_match, if_none_match
    )


@router.put("/{path:path}")
def put_resource(
    path: str,
    backend: BackendDep,
    settings: SettingsDep,
    body: Annotated[bytes, Body()],
    content_type: Annotated[str | None, Header()] = None,
    if_match: Annotated[str | None, Header()] = None,
    if_none_match: Annotated[str | None, Header()] = None,
) -> Response:
    return _put_rdf_resource(
        backend, settings.base_uri, path, body, content_type, if_match, if_none_match
    )


@router.delete("/{path:path}", status_code=204)
def delete_resource(path: str, backend: BackendDep, settings: SettingsDep) -> Response:
    uri = settings.base_uri + path
    try:
        backend.delete(uri)
    except StorageError as exc:
        raise _http_error(exc) from exc
    return Response(status_code=204)


@router.options("/")
def options_root() -> Response:
    return Response(status_code=200, headers={"Allow": ALLOW_RDF})


@router.options("/{path:path}")
def options_resource() -> Response:
    return Response(status_code=200, headers={"Allow": ALLOW_RDF})
