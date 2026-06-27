"""LDP HTTP layer: RDF resource and container endpoints over the storage backend.

Handlers are synchronous: the backend performs blocking rdflib, lock, and
filesystem work, and FastAPI runs sync path operations in a threadpool, which is
the correct execution model for blocking code.
"""

from typing import Annotated

from fastapi import APIRouter, Body, Header, HTTPException, Response
from fastapi.responses import StreamingResponse
from rdflib import Graph, URIRef

from app.config import SettingsDep
from app.ldp.containers import (
    container_kind,
    container_link_types,
    mint_member_uri,
    parent_container_uri,
)
from app.ldp.content import (
    ALLOW_BINARY,
    ALLOW_CONTAINER,
    ALLOW_RDF,
    RDF_CONTENT_TYPES,
    binary_content_type,
    check_preconditions,
    etag_for_binary,
    etag_for_graph,
    etag_for_stream,
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
    LDP_NonRDFSource,
    LDP_RDFSource,
    LDP_Resource,
)

router = APIRouter(tags=["ldp"])

_RDF_LINK = link_header([LDP_Resource, LDP_RDFSource])
_BINARY_LINK = link_header([LDP_Resource, LDP_NonRDFSource])


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


def _get_binary_resource(
    backend: StorageBackend,
    uri: str,
    if_none_match: str | None,
) -> Response | None:
    """Stream the binary resource at *uri*, or return None when it is not binary.

    ``stream_binary`` is the only reliable existence probe for a binary: ``read``
    looks for a ``{uri}.ttl`` that binaries never have. Because the backend yields
    bytes lazily, the generator's existence checks fire only once iterated, so the
    ETag pass over the stream doubles as the probe — ``NotABinaryResource`` means an
    RDF resource lives here (the caller falls through to RDF) and ``ResourceNotFound``
    means nothing exists. Hashing the file on every read is acceptable for a
    personal pod.
    """
    try:
        etag = etag_for_stream(backend.stream_binary(uri))
    except NotABinaryResource:
        return None
    except ResourceNotFound as exc:
        raise HTTPException(status_code=404) from exc
    if if_none_match is not None and if_none_match in (etag, "*"):
        return Response(status_code=304, headers={"ETag": etag})
    return StreamingResponse(
        backend.stream_binary(uri),
        media_type=binary_content_type(backend, uri),
        headers={"ETag": etag, "Link": _BINARY_LINK, "Allow": ALLOW_BINARY},
    )


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


def _get_resource(
    backend: StorageBackend,
    base_uri: str,
    path: str,
    accept: str | None,
    if_none_match: str | None,
) -> Response:
    binary = _get_binary_resource(backend, base_uri + path, if_none_match)
    if binary is not None:
        return binary
    return _get_rdf_resource(backend, base_uri, path, accept, if_none_match)


def _put_rdf_resource(
    backend: StorageBackend,
    base_uri: str,
    path: str,
    body: bytes,
    content_type: str,
    if_match: str | None,
    if_none_match: str | None,
) -> Response:
    uri = base_uri + path
    try:
        read_graph = backend.read(uri)
        current_etag: str | None = etag_for_graph(read_graph)
        stored: Graph | None = read_graph
        exists = True
    except ResourceNotFound:
        stored = None
        current_etag = None
        exists = False
    except StorageError as exc:
        raise _http_error(exc) from exc
    check_preconditions(if_match, if_none_match, current_etag, exists)
    graph = Graph()
    graph.parse(data=body, format=rdflib_format_for(content_type))
    stored_is_container = stored is not None and container_kind(stored, uri) is not None
    if stored_is_container or container_kind(graph, uri) is not None:
        # ldp:contains is server-managed: discard any client-supplied containment
        # and restore the containment recorded on the stored container.
        subject = URIRef(uri)
        graph.remove((None, LDP_contains, None))
        if stored is not None:
            for member in stored.objects(subject, LDP_contains):
                graph.add((subject, LDP_contains, member))
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


def _put_binary_resource(
    backend: StorageBackend,
    base_uri: str,
    path: str,
    body: bytes,
    content_type: str,
    if_match: str | None,
    if_none_match: str | None,
) -> Response:
    uri = base_uri + path
    try:
        current_etag: str | None = etag_for_stream(backend.stream_binary(uri))
        exists = True
    except (NotABinaryResource, ResourceNotFound):
        current_etag = None
        exists = False
    check_preconditions(if_match, if_none_match, current_etag, exists)
    try:
        backend.write_binary(uri, body, content_type)
    except StorageError as exc:
        raise _http_error(exc) from exc
    return Response(
        status_code=200 if exists else 201,
        headers={
            "ETag": etag_for_binary(body),
            "Location": uri,
            "Link": _BINARY_LINK,
            "Allow": ALLOW_BINARY,
        },
    )


def _put_resource(
    backend: StorageBackend,
    base_uri: str,
    path: str,
    body: bytes,
    content_type: str | None,
    if_match: str | None,
    if_none_match: str | None,
) -> Response:
    # A missing Content-Type is treated as opaque bytes per HTTP's octet-stream
    # default; only the three RDF media types take the RDF write path.
    normalized_ct = (content_type or "application/octet-stream").split(";")[0].strip().lower()
    if normalized_ct in RDF_CONTENT_TYPES:
        return _put_rdf_resource(
            backend, base_uri, path, body, normalized_ct, if_match, if_none_match
        )
    return _put_binary_resource(
        backend, base_uri, path, body, normalized_ct, if_match, if_none_match
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
    normalized_ct = (content_type or "application/octet-stream").split(";")[0].strip().lower()
    member_uri = mint_member_uri(container_uri, slug)
    # The container read-modify-write below spans two backend calls and is not
    # atomic at the HTTP level; acceptable for a single-user pod.
    try:
        if normalized_ct in RDF_CONTENT_TYPES:
            member_graph = Graph()
            member_graph.parse(data=body, format=rdflib_format_for(normalized_ct))
            backend.write(member_uri, member_graph)
            etag, link, allow = etag_for_graph(member_graph), _RDF_LINK, ALLOW_RDF
        else:
            backend.write_binary(member_uri, body, normalized_ct)
            etag, link, allow = etag_for_binary(body), _BINARY_LINK, ALLOW_BINARY
        container_graph.add((URIRef(container_uri), LDP_contains, URIRef(member_uri)))
        backend.write(container_uri, container_graph)
    except StorageError as exc:
        raise _http_error(exc) from exc
    return Response(
        status_code=201,
        headers={"Location": member_uri, "ETag": etag, "Link": link, "Allow": allow},
    )


def _detach_from_parent(backend: StorageBackend, base_uri: str, member_uri: str) -> None:
    """Drop ``(parent, ldp:contains, member_uri)`` from the deleted member's parent."""
    parent = parent_container_uri(member_uri, base_uri)
    if parent == member_uri:
        return
    try:
        parent_graph = backend.read(parent)
    except ResourceNotFound:
        return
    except StorageError as exc:
        raise _http_error(exc) from exc
    parent_graph.remove((URIRef(parent), LDP_contains, URIRef(member_uri)))
    try:
        backend.write(parent, parent_graph)
    except StorageError as exc:
        raise _http_error(exc) from exc


@router.api_route("/", methods=["GET", "HEAD"])
def get_root(
    backend: BackendDep,
    settings: SettingsDep,
    accept: Annotated[str | None, Header()] = None,
    if_none_match: Annotated[str | None, Header()] = None,
) -> Response:
    return _get_resource(backend, settings.base_uri, "", accept, if_none_match)


@router.api_route("/{path:path}", methods=["GET", "HEAD"])
def get_resource(
    path: str,
    backend: BackendDep,
    settings: SettingsDep,
    accept: Annotated[str | None, Header()] = None,
    if_none_match: Annotated[str | None, Header()] = None,
) -> Response:
    return _get_resource(backend, settings.base_uri, path, accept, if_none_match)


@router.put("/")
def put_root(
    backend: BackendDep,
    settings: SettingsDep,
    body: Annotated[bytes, Body()],
    content_type: Annotated[str | None, Header()] = None,
    if_match: Annotated[str | None, Header()] = None,
    if_none_match: Annotated[str | None, Header()] = None,
) -> Response:
    return _put_resource(
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
    return _put_resource(
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
    # A binary resource is invisible to read (only its sidecar exists), so a
    # missing read just means "not an RDF container" — delete still adjudicates 404.
    try:
        target: Graph | None = backend.read(uri)
    except ResourceNotFound:
        target = None
    except StorageError as exc:
        raise _http_error(exc) from exc
    if (
        target is not None
        and container_kind(target, uri) is not None
        and next(target.objects(URIRef(uri), LDP_contains), None) is not None
    ):
        raise HTTPException(status_code=409)
    try:
        backend.delete(uri)
    except StorageError as exc:
        raise _http_error(exc) from exc
    _detach_from_parent(backend, settings.base_uri, uri)
    return Response(status_code=204)


@router.options("/")
def options_root() -> Response:
    return Response(status_code=200, headers={"Allow": ALLOW_RDF})


@router.options("/{path:path}")
def options_resource() -> Response:
    return Response(status_code=200, headers={"Allow": ALLOW_RDF})
