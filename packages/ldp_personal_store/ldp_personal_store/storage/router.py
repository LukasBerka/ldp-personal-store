"""Standard-LDP write surface for the view engine's operating state under ``.system/``.

The engine keeps its mutable state — per-grant and per-view delivery counters and the
access log — on the storage server, and updates it with *standard* LDP verbs so the same
state can live on any writable LDP store, not only this one:

* enforcement counters are bumped with a conditional ``PUT`` (``If-Match``), authorized
  for the engine only when the request changes nothing but the enforcement fields;
* the access log grows by ``POST``-ing an entry to its container, the server minting the
  member URI.

There are no bespoke endpoints here anymore — a store that offers conditional ``PUT`` and
container ``POST`` (the LDP defaults) can host this state as-is.
"""

import secrets
from typing import Annotated

from fastapi import APIRouter, Header, HTTPException, Request, Response
from rdflib import Graph, Literal, URIRef
from rdflib.compare import isomorphic
from rdflib.namespace import RDF, XSD

from ldp_personal_store.apidocs import STORAGE_AUTH, UNAUTHORIZED, Responses
from ldp_personal_store.auth.deps import StorageTokenDep
from ldp_personal_store.ldp.content import check_preconditions, etag_for_graph, parse_rdf_body
from ldp_personal_store.ldp.deps import BackendDep, RawBodyDep
from ldp_personal_store.policy.enforce import parse_xsd_datetime
from ldp_personal_store.storage.backend import ResourceNotFound, StorageBackend
from ldp_personal_store.vocab import (
    POD_AccessLogEntry,
    POD_accessLogTimestamp,
    POD_accessLogToken,
    POD_accessLogView,
    POD_enforcementCount,
    POD_lastUsedAt,
)

router = APIRouter(prefix="/.system", tags=["system-internal"])

# The only fields the engine's conditional PUT is permitted to change on a token record;
# any other altered triple means the caller tried to author, not bump a counter.
_TOKEN_ENFORCEMENT_FIELDS: frozenset[URIRef] = frozenset({POD_enforcementCount, POD_lastUsedAt})

_INTERNAL_NOTE = (
    "Engine-internal state write via standard LDP; documented for completeness and split "
    "deployments. A frontend client never calls this."
)


def _without(graph: Graph, subject: URIRef, fields: frozenset[URIRef]) -> Graph:
    """A copy of *graph* with *subject*'s *fields* triples removed (for scope comparison)."""
    out = Graph()
    for s, p, o in graph:
        if not (s == subject and p in fields):
            out.add((s, p, o))
    return out


def guard_enforcement_put(
    backend: StorageBackend,
    uri: str,
    submitted: Graph,
    enforcement_fields: frozenset[URIRef],
    if_match: str | None,
    if_none_match: str | None,
) -> str:
    """Authorize a conditional, enforcement-scoped PUT; return the current ETag.

    The write is a standard LDP conditional PUT from the client's side. On this reference
    store the engine credential is least-privileged: the request must target an existing
    record (no authoring), satisfy ``If-Match`` against the current ETag, and differ from
    what is stored *only* in *enforcement_fields*. Everything else is a 403. The returned
    ETag anchors the atomic ``replace_if_unchanged`` that actually applies the change.
    """
    subject = URIRef(uri)
    try:
        current = backend.read(uri)
    except ResourceNotFound as exc:
        raise HTTPException(status_code=404) from exc
    etag = etag_for_graph(current)
    check_preconditions(if_match, if_none_match, etag, resource_exists=True)
    if not isomorphic(
        _without(current, subject, enforcement_fields),
        _without(submitted, subject, enforcement_fields),
    ):
        raise HTTPException(status_code=403, detail="the engine may modify only enforcement fields")
    return etag


def apply_enforcement_put(
    backend: StorageBackend, uri: str, submitted: Graph, expected_etag: str
) -> Response:
    """Atomically store the validated *submitted* representation, or 412 if it moved."""
    if not backend.replace_if_unchanged(uri, submitted, expected_etag, etag_for_graph):
        raise HTTPException(status_code=412)
    return Response(
        status_code=200,
        headers={"ETag": etag_for_graph(submitted), "Location": uri},
    )


_ENFORCEMENT_RESPONSES: Responses = {
    200: {"description": "Updated; the new ETag identifies the stored representation."},
    401: UNAUTHORIZED,
    403: {"description": "The request would change a field outside the enforcement scope."},
    404: {"description": "No record at this id."},
    412: {"description": "`If-Match` did not match the current representation."},
    415: {"description": "`Content-Type` is not one of the four RDF media types."},
    428: {"description": "Updating a record requires an `If-Match` precondition."},
}


@router.put(
    "/tokens/{record_id}",
    operation_id="putTokenEnforcement",
    summary="Bump a grant's delivery counter (engine-internal)",
    description=_INTERNAL_NOTE,
    response_class=Response,
    responses=_ENFORCEMENT_RESPONSES,
    openapi_extra={"security": STORAGE_AUTH},
)
def put_token_enforcement(
    record_id: str,
    request: Request,
    backend: BackendDep,
    body: RawBodyDep,
    token: StorageTokenDep,
    content_type: Annotated[str | None, Header()] = None,
    if_match: Annotated[str | None, Header()] = None,
    if_none_match: Annotated[str | None, Header()] = None,
) -> Response:
    uri = str(request.app.state.system_ns) + "tokens/" + record_id
    submitted = parse_rdf_body(body, content_type, base_uri=uri)
    etag = guard_enforcement_put(
        backend, uri, submitted, _TOKEN_ENFORCEMENT_FIELDS, if_match, if_none_match
    )
    return apply_enforcement_put(backend, uri, submitted, etag)


@router.post(
    "/access-log",
    status_code=201,
    operation_id="appendAccessLog",
    summary="Append an access-log entry (engine-internal)",
    description=_INTERNAL_NOTE,
    response_class=Response,
    responses={
        201: {"description": "Appended; `Location` names the minted entry."},
        401: UNAUTHORIZED,
        415: {"description": "`Content-Type` is not one of the four RDF media types."},
        422: {"description": "The body does not describe a single access-log entry."},
    },
    openapi_extra={"security": STORAGE_AUTH},
)
def append_access_log(
    request: Request,
    backend: BackendDep,
    body: RawBodyDep,
    token: StorageTokenDep,
    content_type: Annotated[str | None, Header()] = None,
) -> Response:
    entry_uri = str(request.app.state.system_ns) + "access-log/" + secrets.token_urlsafe(8)
    subject = URIRef(entry_uri)
    submitted = parse_rdf_body(body, content_type, base_uri=entry_uri)
    # Store a canonicalized entry, never the posted triples verbatim: the union graph spans
    # every record, so a stray subject in this body — a forged token record above all —
    # would otherwise leak into token/view resolution. Only the minted member, typed as an
    # access-log entry with its three well-formed properties, is persisted.
    if any(s != subject for s, _, _ in submitted):
        raise HTTPException(status_code=422, detail="entry must describe only the posted member")
    if (subject, RDF.type, POD_AccessLogEntry) not in submitted:
        raise HTTPException(status_code=422, detail="body must describe one pod:AccessLogEntry")
    view = submitted.value(subject, POD_accessLogView)
    logged_token = submitted.value(subject, POD_accessLogToken)
    timestamp = submitted.value(subject, POD_accessLogTimestamp)
    if not (
        isinstance(view, URIRef) and isinstance(logged_token, URIRef) and timestamp is not None
    ):
        raise HTTPException(status_code=422, detail="access-log entry is missing a property")
    try:
        parse_xsd_datetime(str(timestamp))
    except ValueError as exc:
        raise HTTPException(
            status_code=422, detail="accessLogTimestamp is not an xsd:dateTime"
        ) from exc

    entry = Graph()
    entry.add((subject, RDF.type, POD_AccessLogEntry))
    entry.add((subject, POD_accessLogView, view))
    entry.add((subject, POD_accessLogToken, logged_token))
    entry.add((subject, POD_accessLogTimestamp, Literal(str(timestamp), datatype=XSD.dateTime)))
    backend.write_system(entry_uri, entry)
    return Response(status_code=201, headers={"Location": entry_uri})
