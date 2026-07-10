"""LDP HTTP layer: RDF resource and container endpoints over the storage backend."""

from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from fastapi.responses import StreamingResponse
from rdflib import Graph, URIRef
from rdflib.namespace import RDF

from ldp_common.apidocs import (
    ADMIN_AUTH,
    STORAGE_AUTH,
    UNAUTHORIZED,
    Responses,
    rdf_content,
    rdf_response,
)
from ldp_common.config import SettingsDep
from ldp_common.rdfcontent import (
    ACCEPT_POST,
    ALLOW_BINARY,
    ALLOW_CONTAINER,
    ALLOW_RDF,
    RDF_CONTENT_TYPES,
    check_preconditions,
    container_prefer,
    etag_for_binary,
    etag_for_graph,
    etag_for_stream,
    link_header,
    negotiate,
    normalize_media_type,
    parse_rdf_body,
)
from ldp_common.vocab import (
    LDP_Container,
    LDP_contains,
    LDP_hasMemberRelation,
    LDP_isMemberOfRelation,
    LDP_membershipResource,
    LDP_NonRDFSource,
    LDP_RDFSource,
    LDP_Resource,
)
from ldp_personal_store.auth.deps import get_admin_token, get_storage_token
from ldp_personal_store.ldp.containers import (
    container_kind,
    container_link_types,
    mint_member_uri,
    parent_container_uri,
)
from ldp_personal_store.ldp.content import binary_content_type
from ldp_personal_store.ldp.deps import BackendDep, http_error
from ldp_personal_store.storage.backend import (
    NotABinaryResource,
    ResourceNotFound,
    StorageBackend,
    StorageError,
)

router = APIRouter(tags=["ldp"])

# Read-side routes accept either administrative credential; write-side routes are
# owner-only. Attached per route because a router-level dependency cannot vary by verb.
_READ = [Depends(get_storage_token)]
_WRITE = [Depends(get_admin_token)]

_RDF_LINK = link_header([LDP_Resource, LDP_RDFSource])
_BINARY_LINK = link_header([LDP_Resource, LDP_NonRDFSource])


def _binary_link(uri: str) -> str:
    """``Link`` header for the LDP-NR at *uri*."""
    return f'{_BINARY_LINK}, <{uri}.meta>; rel="describedby"; anchor="{uri}"'


# OpenAPI
_GET_DESCRIPTION = """\
Dereference the resource at this pod-relative path: an RDF document, an LDP container, \
or a stored binary.

* RDF responses are negotiated via `Accept` among the four supported serializations \
(default `text/turtle`). Binaries return the media type recorded when they were stored \
and ignore `Accept`.
* Container representations list members as `ldp:contains` triples; Direct Containers \
additionally synthesize their membership triples into the response. A `Prefer: \
return=representation` header with `include`/`omit` of `ldp:PreferContainment`, \
`ldp:PreferMembership`, or `ldp:PreferMinimalContainer` tailors which of those appear; \
when honored the response echoes `Preference-Applied: return=representation`.
* Every response carries an `ETag` (echo it in `If-None-Match` to get `304`), a `Link` \
header advertising the resource's LDP types (`rel="type"`), and an `Allow` header naming \
the verbs the resource supports.
* `HEAD` is also accepted: same status and headers, no body.
"""

_GET_RESPONSES: Responses = {
    200: rdf_response(
        "The representation. A binary streams as its stored media type instead "
        "(any type, not only the RDF four)."
    ),
    304: {"description": "Representation unchanged (`If-None-Match` matched); no body."},
    401: UNAUTHORIZED,
    404: {"description": "No resource at this path."},
}

_PUT_DESCRIPTION = """\
Create or replace the resource at this exact path.

* `Content-Type` selects the write path: one of the four RDF media types is parsed and \
stored as an RDF resource; any other type (or none) is stored verbatim as a binary.
* A body whose subject is typed `ldp:BasicContainer` (or another container type) creates \
a container.
* Preconditions: replacing an existing resource **requires** `If-Match: <etag>` (a bare \
update is refused with `428`) so a blind overwrite cannot clobber a concurrent change; \
`If-None-Match: *` makes the request create-only.
* Containment is server-managed: `ldp:contains` triples in the body are ignored when they \
echo the stored set and **rejected with `409`** when they differ (stored containment is \
preserved), and a newly created resource is added to its parent container's listing \
automatically.
"""

_PUT_BODY = {
    "required": True,
    "description": (
        "The new representation: RDF in one of the four supported media types, or any "
        "other media type to store the bytes as a binary."
    ),
    "content": {
        **rdf_content(
            "@prefix dcterms: <http://purl.org/dc/terms/> .\n\n"
            '<> dcterms:title "Meeting notes" ;\n'
            '   dcterms:description "Notes from the 2026-07-01 sync" .'
        ),
        "application/octet-stream": {"schema": {"type": "string", "format": "binary"}},
    },
}

_PUT_RESPONSES: Responses = {
    200: {"description": "Replaced. Headers as on create."},
    201: {
        "description": (
            "Created. `Location` names the resource; `ETag` identifies the stored representation."
        )
    },
    400: {"description": "The RDF body does not parse in the declared `Content-Type`."},
    401: UNAUTHORIZED,
    403: {"description": "The path lies under the reserved `.system/` prefix."},
    409: {"description": "The body would modify the container's server-managed `ldp:contains`."},
    412: {"description": "`If-Match` / `If-None-Match` precondition failed."},
    428: {"description": "Updating an existing resource requires an `If-Match` precondition."},
}

_POST_DESCRIPTION = """\
Create a member inside the container at this path.

The member URI is minted by the server (a `Slug` header is honored when free); it is \
returned in `Location` and added to the container's `ldp:contains`. The body follows the \
same RDF/binary split as `PUT`.
"""

_POST_RESPONSES: Responses = {
    201: {"description": "Member created; `Location` names it."},
    400: {"description": "The RDF body does not parse in the declared `Content-Type`."},
    401: UNAUTHORIZED,
    403: {"description": "The path lies under the reserved `.system/` prefix."},
    404: {"description": "No resource at this path."},
    405: {"description": "The target exists but is not a container; `Allow` lists its verbs."},
}

_DELETE_RESPONSES: Responses = {
    204: {"description": "Deleted; the parent container's listing is updated."},
    401: UNAUTHORIZED,
    403: {"description": "The path lies under the reserved `.system/` prefix."},
    404: {"description": "No resource at this path."},
    409: {"description": "The container still has members; delete them first."},
}

_OPTIONS_RESPONSES: Responses = {
    200: {
        "description": (
            "`Allow` lists the verbs this specific resource supports (containers add "
            "`POST`, binaries drop it)."
        )
    },
    401: UNAUTHORIZED,
    404: {"description": "No resource at this path."},
}


async def raw_body(request: Request) -> bytes:
    return await request.body()


def _head_of(response: Response) -> Response:
    return Response(status_code=response.status_code, headers=dict(response.headers))


def _container_representation(
    graph: Graph,
    uri: str,
    include_containment: bool = True,
    include_membership: bool = True,
) -> Graph:
    """Return a response copy of a container *graph* with LDP types synthesized."""
    subject = URIRef(uri)
    result = Graph()
    for triple in graph:
        if not include_containment and triple[0] == subject and triple[1] == LDP_contains:
            continue
        result.add(triple)
    result.add((subject, RDF.type, LDP_Resource))
    result.add((subject, RDF.type, LDP_RDFSource))
    result.add((subject, RDF.type, LDP_Container))
    membership_resource = graph.value(subject, LDP_membershipResource)
    if include_membership and membership_resource is not None:
        has_member = graph.value(subject, LDP_hasMemberRelation)
        is_member_of = graph.value(subject, LDP_isMemberOfRelation)
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
    """Stream the binary resource at *uri*, or return None when it is not binary."""
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
        headers={"ETag": etag, "Link": _binary_link(uri), "Allow": ALLOW_BINARY},
    )


def _get_rdf_resource(
    backend: StorageBackend,
    base_uri: str,
    path: str,
    accept: str | None,
    if_none_match: str | None,
    prefer: str | None = None,
) -> Response:
    uri = base_uri + path
    try:
        graph = backend.read(uri)
    except StorageError as exc:
        raise http_error(exc) from exc
    fmt, media_type = negotiate(accept)
    etag = etag_for_graph(graph)
    if if_none_match is not None and if_none_match in (etag, "*"):
        return Response(status_code=304, headers={"ETag": etag})
    kind = container_kind(graph, uri)
    headers = {"ETag": etag}
    if kind is None:
        # A plain LDP-RS round-trips verbatim; its LDP types travel in the Link header.
        body_graph = graph
        headers["Link"], headers["Allow"] = _RDF_LINK, ALLOW_RDF
    else:
        include_containment, include_membership, prefer_applied = container_prefer(prefer)
        body_graph = _container_representation(graph, uri, include_containment, include_membership)
        headers["Link"] = link_header(container_link_types(kind))
        headers["Allow"], headers["Accept-Post"] = ALLOW_CONTAINER, ACCEPT_POST
        if prefer_applied:
            headers["Preference-Applied"] = "return=representation"
    return Response(
        content=body_graph.serialize(format=fmt),
        media_type=media_type,
        headers=headers,
    )


def _get_resource(
    backend: StorageBackend,
    base_uri: str,
    path: str,
    accept: str | None,
    if_none_match: str | None,
    prefer: str | None = None,
) -> Response:
    binary = _get_binary_resource(backend, base_uri + path, if_none_match)
    if binary is not None:
        return binary
    return _get_rdf_resource(backend, base_uri, path, accept, if_none_match, prefer)


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
        raise http_error(exc) from exc
    check_preconditions(if_match, if_none_match, current_etag, exists)
    graph = parse_rdf_body(body, content_type, base_uri=uri)
    stored_is_container = stored is not None and container_kind(stored, uri) is not None
    if stored_is_container or container_kind(graph, uri) is not None:
        subject = URIRef(uri)
        if stored_is_container and stored is not None:
            # Containment is server-managed. Supplying a different
            # set is an attempt to modify containment and is refused as per LDP standards
            client_contains = set(graph.objects(subject, LDP_contains))
            stored_contains = set(stored.objects(subject, LDP_contains))
            if client_contains and client_contains != stored_contains:
                raise HTTPException(
                    status_code=409,
                    detail="PUT must not modify server-managed containment triples",
                )
        # ldp:contains is server-managed: discard any client-supplied containment.
        graph.remove((None, LDP_contains, None))
        if stored is not None:
            for member in stored.objects(subject, LDP_contains):
                graph.add((subject, LDP_contains, member))
        # Membership triples are server-derived from containment and synthesized only in responses.
        membership_resource = graph.value(subject, LDP_membershipResource)
        if membership_resource is not None:
            has_member = graph.value(subject, LDP_hasMemberRelation)
            is_member_of = graph.value(subject, LDP_isMemberOfRelation)
            if has_member is not None:
                graph.remove((membership_resource, has_member, None))
            if is_member_of is not None:
                graph.remove((None, is_member_of, membership_resource))
    try:
        backend.write(uri, graph)
    except StorageError as exc:
        raise http_error(exc) from exc
    if not exists:
        _attach_to_parent(backend, base_uri, uri)
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
    except NotABinaryResource, ResourceNotFound:
        current_etag = None
        exists = False
    check_preconditions(if_match, if_none_match, current_etag, exists)
    try:
        backend.write_binary(uri, body, content_type)
    except StorageError as exc:
        raise http_error(exc) from exc
    if not exists:
        _attach_to_parent(backend, base_uri, uri)
    return Response(
        status_code=200 if exists else 201,
        headers={
            "ETag": etag_for_binary(body),
            "Location": uri,
            "Link": _binary_link(uri),
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
    normalized_ct = normalize_media_type(content_type, "application/octet-stream")
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
        raise http_error(exc) from exc
    if container_kind(container_graph, container_uri) is None:
        raise HTTPException(status_code=405, headers={"Allow": ALLOW_RDF})
    normalized_ct = normalize_media_type(content_type, "application/octet-stream")
    member_uri = mint_member_uri(container_uri, slug)
    try:
        if normalized_ct in RDF_CONTENT_TYPES:
            member_graph = parse_rdf_body(body, normalized_ct, base_uri=member_uri)
            backend.write(member_uri, member_graph)
            etag, link, allow = etag_for_graph(member_graph), _RDF_LINK, ALLOW_RDF
        else:
            backend.write_binary(member_uri, body, normalized_ct)
            etag, link, allow = etag_for_binary(body), _binary_link(member_uri), ALLOW_BINARY
        container_graph.add((URIRef(container_uri), LDP_contains, URIRef(member_uri)))
        backend.write(container_uri, container_graph)
    except StorageError as exc:
        raise http_error(exc) from exc
    return Response(
        status_code=201,
        headers={"Location": member_uri, "ETag": etag, "Link": link, "Allow": allow},
    )


def _attach_to_parent(backend: StorageBackend, base_uri: str, member_uri: str) -> None:
    """Add ``(parent, ldp:contains, member_uri)`` for a PUT-created member."""
    parent = parent_container_uri(member_uri, base_uri)
    if parent == member_uri:
        return
    try:
        parent_graph = backend.read(parent)
    except ResourceNotFound:
        return
    except StorageError as exc:
        raise http_error(exc) from exc
    if container_kind(parent_graph, parent) is None:
        return
    parent_graph.add((URIRef(parent), LDP_contains, URIRef(member_uri)))
    try:
        backend.write(parent, parent_graph)
    except StorageError as exc:
        raise http_error(exc) from exc


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
        raise http_error(exc) from exc
    parent_graph.remove((URIRef(parent), LDP_contains, URIRef(member_uri)))
    try:
        backend.write(parent, parent_graph)
    except StorageError as exc:
        raise http_error(exc) from exc


@router.get(
    "/",
    dependencies=_READ,
    operation_id="getRoot",
    summary="Read the pod root container",
    description=_GET_DESCRIPTION,
    response_class=Response,
    responses=_GET_RESPONSES,
    openapi_extra={"security": STORAGE_AUTH},
)
def get_root(
    backend: BackendDep,
    settings: SettingsDep,
    accept: Annotated[str | None, Header()] = None,
    if_none_match: Annotated[str | None, Header()] = None,
    prefer: Annotated[str | None, Header()] = None,
) -> Response:
    return _get_resource(backend, settings.base_uri, "", accept, if_none_match, prefer)


@router.get(
    "/{path:path}",
    dependencies=_READ,
    operation_id="getResource",
    summary="Read a resource, container, or binary",
    description=_GET_DESCRIPTION,
    response_class=Response,
    responses=_GET_RESPONSES,
    openapi_extra={"security": STORAGE_AUTH},
)
def get_resource(
    path: str,
    backend: BackendDep,
    settings: SettingsDep,
    accept: Annotated[str | None, Header()] = None,
    if_none_match: Annotated[str | None, Header()] = None,
    prefer: Annotated[str | None, Header()] = None,
) -> Response:
    return _get_resource(backend, settings.base_uri, path, accept, if_none_match, prefer)


@router.head("/", dependencies=_READ, include_in_schema=False)
def head_root(
    backend: BackendDep,
    settings: SettingsDep,
    accept: Annotated[str | None, Header()] = None,
    if_none_match: Annotated[str | None, Header()] = None,
    prefer: Annotated[str | None, Header()] = None,
) -> Response:
    return _head_of(_get_resource(backend, settings.base_uri, "", accept, if_none_match, prefer))


@router.head("/{path:path}", dependencies=_READ, include_in_schema=False)
def head_resource(
    path: str,
    backend: BackendDep,
    settings: SettingsDep,
    accept: Annotated[str | None, Header()] = None,
    if_none_match: Annotated[str | None, Header()] = None,
    prefer: Annotated[str | None, Header()] = None,
) -> Response:
    return _head_of(_get_resource(backend, settings.base_uri, path, accept, if_none_match, prefer))


@router.put(
    "/",
    dependencies=_WRITE,
    operation_id="putRoot",
    summary="Replace the pod root container's description",
    description=_PUT_DESCRIPTION,
    response_class=Response,
    responses=_PUT_RESPONSES,
    openapi_extra={"security": ADMIN_AUTH, "requestBody": _PUT_BODY},
)
def put_root(
    backend: BackendDep,
    settings: SettingsDep,
    body: Annotated[bytes, Depends(raw_body)],
    content_type: Annotated[str | None, Header()] = None,
    if_match: Annotated[str | None, Header()] = None,
    if_none_match: Annotated[str | None, Header()] = None,
) -> Response:
    return _put_resource(
        backend, settings.base_uri, "", body, content_type, if_match, if_none_match
    )


@router.put(
    "/{path:path}",
    dependencies=_WRITE,
    operation_id="putResource",
    summary="Create or replace a resource at a chosen path",
    description=_PUT_DESCRIPTION,
    response_class=Response,
    responses=_PUT_RESPONSES,
    openapi_extra={"security": ADMIN_AUTH, "requestBody": _PUT_BODY},
)
def put_resource(
    path: str,
    backend: BackendDep,
    settings: SettingsDep,
    body: Annotated[bytes, Depends(raw_body)],
    content_type: Annotated[str | None, Header()] = None,
    if_match: Annotated[str | None, Header()] = None,
    if_none_match: Annotated[str | None, Header()] = None,
) -> Response:
    return _put_resource(
        backend, settings.base_uri, path, body, content_type, if_match, if_none_match
    )


@router.post(
    "/",
    dependencies=_WRITE,
    operation_id="postToRoot",
    summary="Create a member in the pod root container",
    description=_POST_DESCRIPTION,
    response_class=Response,
    responses=_POST_RESPONSES,
    openapi_extra={"security": ADMIN_AUTH, "requestBody": _PUT_BODY},
)
def post_root(
    backend: BackendDep,
    settings: SettingsDep,
    body: Annotated[bytes, Depends(raw_body)],
    content_type: Annotated[str | None, Header()] = None,
    slug: Annotated[str | None, Header()] = None,
) -> Response:
    return _post_member(backend, settings.base_uri, "", body, content_type, slug)


@router.post(
    "/{path:path}",
    dependencies=_WRITE,
    operation_id="postToContainer",
    summary="Create a member in a container",
    description=_POST_DESCRIPTION,
    response_class=Response,
    responses=_POST_RESPONSES,
    openapi_extra={"security": ADMIN_AUTH, "requestBody": _PUT_BODY},
)
def post_resource(
    path: str,
    backend: BackendDep,
    settings: SettingsDep,
    body: Annotated[bytes, Depends(raw_body)],
    content_type: Annotated[str | None, Header()] = None,
    slug: Annotated[str | None, Header()] = None,
) -> Response:
    return _post_member(backend, settings.base_uri, path, body, content_type, slug)


@router.delete(
    "/{path:path}",
    status_code=204,
    dependencies=_WRITE,
    operation_id="deleteResource",
    summary="Delete a resource, empty container, or binary",
    description=(
        "Remove the resource at this path and drop it from its parent container's "
        "listing. A container must be emptied of members first."
    ),
    response_class=Response,
    responses=_DELETE_RESPONSES,
    openapi_extra={"security": ADMIN_AUTH},
)
def delete_resource(path: str, backend: BackendDep, settings: SettingsDep) -> Response:
    uri = settings.base_uri + path
    # A binary resource is invisible to read (only its sidecar exists), so a
    # missing read just means "not an RDF container" — delete still adjudicates 404.
    try:
        target: Graph | None = backend.read(uri)
    except ResourceNotFound:
        target = None
    except StorageError as exc:
        raise http_error(exc) from exc
    if (
        target is not None
        and container_kind(target, uri) is not None
        and next(target.objects(URIRef(uri), LDP_contains), None) is not None
    ):
        raise HTTPException(status_code=409)
    try:
        backend.delete(uri)
    except StorageError as exc:
        raise http_error(exc) from exc
    _detach_from_parent(backend, settings.base_uri, uri)
    return Response(status_code=204)


def _options_response(backend: StorageBackend, base_uri: str, path: str) -> Response:
    """Answer OPTIONS with the Allow set the resource actually supports."""
    uri = base_uri + path
    is_container = False
    is_binary = False
    try:
        graph = backend.read(uri)
        is_container = container_kind(graph, uri) is not None
        allow = ALLOW_CONTAINER if is_container else ALLOW_RDF
    except ResourceNotFound:
        # Not RDF: probe for a binary. The stream's existence checks fire lazily,
        # so pulling one chunk is the probe; no bytes are sent to the client.
        try:
            next(backend.stream_binary(uri), None)
        except StorageError as exc:
            raise HTTPException(status_code=404) from exc
        allow = ALLOW_BINARY
        is_binary = True
    except StorageError as exc:
        raise http_error(exc) from exc
    headers = {"Allow": allow}
    if is_container:
        headers["Accept-Post"] = ACCEPT_POST
    if is_binary:
        # Match the LDP-NR GET so its describedby pointer is discoverable via OPTIONS.
        headers["Link"] = _binary_link(uri)
    return Response(status_code=200, headers=headers)


@router.options(
    "/",
    dependencies=_READ,
    operation_id="optionsRoot",
    summary="Capabilities of the pod root",
    description="Report, via the `Allow` header, the verbs the pod root supports.",
    response_class=Response,
    responses=_OPTIONS_RESPONSES,
    openapi_extra={"security": STORAGE_AUTH},
)
def options_root(backend: BackendDep, settings: SettingsDep) -> Response:
    return _options_response(backend, settings.base_uri, "")


@router.options(
    "/{path:path}",
    dependencies=_READ,
    operation_id="optionsResource",
    summary="Capabilities of a resource",
    description=(
        "Report, via the `Allow` header, the verbs the resource at this path actually "
        "supports: containers add `POST`, binaries drop it."
    ),
    response_class=Response,
    responses=_OPTIONS_RESPONSES,
    openapi_extra={"security": STORAGE_AUTH},
)
def options_resource(path: str, backend: BackendDep, settings: SettingsDep) -> Response:
    return _options_response(backend, settings.base_uri, path)
