"""Storage-side request-path write surface under ``.system/``."""

import secrets

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, field_validator
from rdflib import Graph, Literal, URIRef
from rdflib.namespace import RDF, XSD

from ldp_personal_store.apidocs import STORAGE_AUTH, UNAUTHORIZED, Responses
from ldp_personal_store.auth.deps import StorageTokenDep
from ldp_personal_store.ldp.deps import BackendDep
from ldp_personal_store.policy.enforce import parse_xsd_datetime
from ldp_personal_store.storage.backend import ResourceNotFound
from ldp_personal_store.vocab import (
    POD_AccessLogEntry,
    POD_accessLogTimestamp,
    POD_accessLogToken,
    POD_accessLogView,
)

router = APIRouter(prefix="/.system", tags=["system-internal"])


def _valid_xsd_datetime(value: str) -> str:
    parse_xsd_datetime(value)
    return value


class TokenEnforcementUpdate(BaseModel):
    count: int
    last_used_at: str

    _check_last_used_at = field_validator("last_used_at")(_valid_xsd_datetime)


class ViewEnforcementUpdate(BaseModel):
    count: int


class AccessLogAppend(BaseModel):
    view_uri: str
    token_uri: str
    timestamp: str

    _check_timestamp = field_validator("timestamp")(_valid_xsd_datetime)


_INTERNAL_NOTE = (
    "Internal engine-plane write, documented for completeness and split deployments; "
    "a frontend client never calls this."
)

_INTERNAL_RESPONSES: Responses = {
    204: {"description": "Recorded."},
    401: UNAUTHORIZED,
    404: {"description": "No record at this id."},
}


@router.post(
    "/tokens/{record_id}/enforcement",
    status_code=204,
    operation_id="bumpTokenEnforcement",
    summary="Update a grant's delivery counter (engine-internal)",
    description=_INTERNAL_NOTE,
    response_class=Response,
    responses=_INTERNAL_RESPONSES,
    openapi_extra={"security": STORAGE_AUTH},
)
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


@router.post(
    "/views/{view_id}/enforcement",
    status_code=204,
    operation_id="bumpViewEnforcement",
    summary="Update a view's delivery counter (engine-internal)",
    description=_INTERNAL_NOTE,
    response_class=Response,
    responses=_INTERNAL_RESPONSES,
    openapi_extra={"security": STORAGE_AUTH},
)
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


@router.post(
    "/access-log",
    status_code=204,
    operation_id="appendAccessLog",
    summary="Append an access-log entry (engine-internal)",
    description=_INTERNAL_NOTE,
    response_class=Response,
    responses={204: {"description": "Appended."}, 401: UNAUTHORIZED},
    openapi_extra={"security": STORAGE_AUTH},
)
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
