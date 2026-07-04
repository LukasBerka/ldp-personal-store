"""Storage-side request-path write surface under ``.system/``.

These three endpoints are the entire write access the engine's credential carries:
bumping the mutable enforcement fields on a token or view record, and appending an
immutable access-log entry for a confirmed delivery. Each maps 1:1 onto a backend
primitive that rewrites only the mutable fields, so neither credential can reshape
a record through this surface. The pod owner's admin token is accepted too — the
owner's authority is a superset of the engine's.

Log entries are independent resources under ``.system/access-log/`` (a fresh id per
append), so appending is a plain single write and the statistics layer aggregates
them with one SPARQL query over the union graph.
"""

import secrets

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel
from rdflib import Graph, Literal, URIRef
from rdflib.namespace import RDF, XSD

from app.auth.deps import StorageTokenDep
from app.ldp.deps import BackendDep
from app.storage.backend import ResourceNotFound
from app.vocab import (
    POD_AccessLogEntry,
    POD_accessLogTimestamp,
    POD_accessLogToken,
    POD_accessLogView,
)

router = APIRouter(prefix="/.system", tags=["system-internal"])


class TokenEnforcementUpdate(BaseModel):
    count: int
    last_used_at: str


class ViewEnforcementUpdate(BaseModel):
    count: int


class AccessLogAppend(BaseModel):
    view_uri: str
    token_uri: str
    timestamp: str


@router.post("/tokens/{record_id}/enforcement", status_code=204)
def bump_token_enforcement(
    record_id: str,
    request: Request,
    backend: BackendDep,
    body: TokenEnforcementUpdate,
    token: StorageTokenDep,
) -> Response:
    uri = str(request.app.state.system_ns) + "tokens/" + record_id
    try:
        backend.read(uri)
    except ResourceNotFound as exc:
        raise HTTPException(status_code=404) from exc
    backend.update_enforcement(uri, body.count, body.last_used_at)
    return Response(status_code=204)


@router.post("/views/{view_id}/enforcement", status_code=204)
def bump_view_enforcement(
    view_id: str,
    request: Request,
    backend: BackendDep,
    body: ViewEnforcementUpdate,
    token: StorageTokenDep,
) -> Response:
    uri = str(request.app.state.system_ns) + "views/" + view_id
    try:
        backend.read(uri)
    except ResourceNotFound as exc:
        raise HTTPException(status_code=404) from exc
    backend.update_view_enforcement(uri, body.count)
    return Response(status_code=204)


@router.post("/access-log", status_code=204)
def append_access_log(
    request: Request,
    backend: BackendDep,
    body: AccessLogAppend,
    token: StorageTokenDep,
) -> Response:
    entry_uri = str(request.app.state.system_ns) + "access-log/" + secrets.token_urlsafe(8)
    subject = URIRef(entry_uri)
    graph = Graph()
    graph.add((subject, RDF.type, POD_AccessLogEntry))
    graph.add((subject, POD_accessLogView, URIRef(body.view_uri)))
    graph.add((subject, POD_accessLogToken, URIRef(body.token_uri)))
    graph.add((subject, POD_accessLogTimestamp, Literal(body.timestamp, datatype=XSD.dateTime)))
    backend.write_system(entry_uri, graph)
    return Response(status_code=204)
