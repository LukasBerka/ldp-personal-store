"""LDP HTTP layer: RDF resource and container endpoints over the storage backend.

Handlers are synchronous: the backend performs blocking rdflib, lock, and
filesystem work, and FastAPI runs sync path operations in a threadpool, which is
the correct execution model for blocking code.
"""

from typing import Annotated

from fastapi import APIRouter, Body, Header, HTTPException, Response
from rdflib import Graph, URIRef

from app.config import SettingsDep
from app.ldp.containers import container_kind, container_link_types, mint_member_uri
from app.ldp.content import (
    ALLOW_CONTAINER,
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
from app.vocab import (
    LDP_contains,
    LDP_hasMemberRelation,
    LDP_isMemberOfRelation,
    LDP_membershipResource,
    LDP_RDFSource,
    LDP_Resource,
)

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


def _with_direct_membership(graph: Graph, uri: str) -> Graph:
    """Return a serialization copy of *graph* with Direct membership triples added.

    Membership is derived from the stored ``ldp:contains`` triples plus the
    container's ``membershipResource`` and member-relation predicate; the result
    is used only for the response body and is never persisted.
    """
    subject = URIRef(uri)
    membership_resource = graph.value(subject, LDP_membershipResource)
    if membership_resource is None:
        return graph
    has_member = graph.value(subject, LDP_hasMemberRelation)
    is_member_of = graph.value(subject, LDP_isMemberOfRelation)
    result = Graph()
    for triple in graph:
        result.add(triple)
    for member in graph.objects(subject, LDP_contains):
        if has_member is not None:
            result.add((membership_resource, has_member, member))
        elif is_member_of is not None:
            result.add((member, is_member_of, membership_resource))
    return result


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
    kind = container_kind(graph, uri)
    if kind is None:
        link, allow, body_graph = _RDF_LINK, ALLOW_RDF, graph
    else:
        link, allow = link_header(container_link_types(kind)), ALLOW_CONTAINER
        body_graph = _with_direct_membership(graph, uri) if kind == "direct" else graph
    return Response(
        content=serialize_graph(body_graph, fmt),
        media_type=media_type,
        headers={"ETag": etag, "Link": link, "Allow": allow},
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


def _post_member(
    backend: StorageBackend,
    base_uri: str,
    path: str,
    body: bytes,
    content_type: str | None,
    slug: str | None,
) -> Response:
    container_uri = base_uri + path
    try:
        container_graph = backend.read(container_uri)
    except StorageError as exc:
        raise _http_error(exc) from exc
    if container_kind(container_graph, container_uri) is None:
        raise HTTPException(status_code=405, headers={"Allow": ALLOW_RDF})
    normalized_ct = (content_type or "text/turtle").split(";")[0].strip().lower()
    if normalized_ct not in RDF_CONTENT_TYPES:
        raise HTTPException(status_code=415, detail=f"Unsupported media type {normalized_ct!r}")
    member_uri = mint_member_uri(container_uri, slug)
    member_graph = Graph()
    member_graph.parse(data=body, format=rdflib_format_for(normalized_ct))
    # The container read-modify-write below spans two backend calls and is not
    # atomic at the HTTP level; acceptable for a single-user pod.
    try:
        backend.write(member_uri, member_graph)
        container_graph.add((URIRef(container_uri), LDP_contains, URIRef(member_uri)))
        backend.write(container_uri, container_graph)
    except StorageError as exc:
        raise _http_error(exc) from exc
    return Response(
        status_code=201,
        headers={
            "Location": member_uri,
            "ETag": etag_for_graph(member_graph),
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


@router.post("/")
def post_root(
    backend: BackendDep,
    settings: SettingsDep,
    body: Annotated[bytes, Body()],
    content_type: Annotated[str | None, Header()] = None,
    slug: Annotated[str | None, Header()] = None,
) -> Response:
    return _post_member(backend, settings.base_uri, "", body, content_type, slug)


@router.post("/{path:path}")
def post_resource(
    path: str,
    backend: BackendDep,
    settings: SettingsDep,
    body: Annotated[bytes, Body()],
    content_type: Annotated[str | None, Header()] = None,
    slug: Annotated[str | None, Header()] = None,
) -> Response:
    return _post_member(backend, settings.base_uri, path, body, content_type, slug)


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
