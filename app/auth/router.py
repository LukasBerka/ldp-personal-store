"""Management router for the reserved ``.system/`` subtree.

Mounted ahead of the public LDP catch-all so ``.system/`` paths are adjudicated
here instead of reaching the public handlers. Writes — issuing consumer tokens,
authoring policies, revoking records — require the pod owner's admin token. Reads
accept either administrative credential, but the engine's token is scoped to the
record kinds the request path needs (``views/`` and ``tokens/``, which includes
``tokens/policies/``); creation, deletion, and wholesale rewriting of system
resources remain in the pod owner's hands.
"""

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel
from rdflib import Graph, Literal, URIRef
from rdflib.namespace import RDF, XSD

from app.auth.deps import AdminTokenDep, StorageTokenDep
from app.auth.tokens import mint_token, revoke_token
from app.config import SettingsDep
from app.ldp.deps import BackendDep
from app.storage.backend import ResourceNotFound, StorageError
from app.vocab import (
    POD_EngineToken,
    POD_expiresAt,
    POD_maxRetrievals,
    POD_minInterval,
    POD_Policy,
    POD_validFrom,
    POD_validUntil,
)

router = APIRouter(prefix="/.system", tags=["system"])

_ENGINE_READABLE_PREFIXES = ("views/", "tokens/")


class TokenIssueRequest(BaseModel):
    # A single grant may unlock any number of views (FR4); empty means unscoped.
    linked_view_uris: list[str] = []


class TokenIssueResponse(BaseModel):
    # The plaintext is unrecoverable: it is surfaced here exactly once and never
    # persisted — only its SHA-256 hash is stored on the record.
    token: str
    record_uri: str


class PolicyWriteRequest(BaseModel):
    expires_at: str | None = None
    valid_from: str | None = None
    valid_until: str | None = None
    max_retrievals: int | None = None
    min_interval: int | None = None


class PolicyWriteResponse(BaseModel):
    policy_uri: str


def _http_error(exc: StorageError) -> HTTPException:
    if isinstance(exc, ResourceNotFound):
        return HTTPException(status_code=404)
    return HTTPException(status_code=500)


@router.post("/tokens")
def issue_token(
    request: Request,
    backend: BackendDep,
    body: TokenIssueRequest,
    token: AdminTokenDep,
) -> TokenIssueResponse:
    plaintext, record_uri = mint_token(
        backend, request.app.state.system_ns, body.linked_view_uris
    )
    return TokenIssueResponse(token=plaintext, record_uri=record_uri)


@router.put("/tokens/policies/{policy_id}")
def write_policy(
    policy_id: str,
    request: Request,
    backend: BackendDep,
    body: PolicyWriteRequest,
    token: AdminTokenDep,
) -> PolicyWriteResponse:
    # A full PUT replaces the policy graph: omitted constraints are cleared, not merged.
    policy_uri = str(request.app.state.system_ns) + "tokens/policies/" + policy_id
    subject = URIRef(policy_uri)
    graph = Graph()
    graph.add((subject, RDF.type, POD_Policy))
    if body.expires_at is not None:
        graph.add((subject, POD_expiresAt, Literal(body.expires_at, datatype=XSD.dateTime)))
    if body.valid_from is not None:
        graph.add((subject, POD_validFrom, Literal(body.valid_from, datatype=XSD.dateTime)))
    if body.valid_until is not None:
        graph.add((subject, POD_validUntil, Literal(body.valid_until, datatype=XSD.dateTime)))
    if body.max_retrievals is not None:
        graph.add((subject, POD_maxRetrievals, Literal(body.max_retrievals, datatype=XSD.integer)))
    if body.min_interval is not None:
        graph.add((subject, POD_minInterval, Literal(body.min_interval, datatype=XSD.integer)))
    backend.write_system(policy_uri, graph)
    return PolicyWriteResponse(policy_uri=policy_uri)


@router.get("/{path:path}")
def read_system(
    path: str, backend: BackendDep, settings: SettingsDep, token: StorageTokenDep
) -> Response:
    if token.token_type == str(POD_EngineToken) and not path.startswith(
        _ENGINE_READABLE_PREFIXES
    ):
        raise HTTPException(status_code=403)
    uri = settings.base_uri + ".system/" + path
    try:
        graph = backend.read(uri)
    except StorageError as exc:
        raise _http_error(exc) from exc
    return Response(content=graph.serialize(format="turtle"), media_type="text/turtle")


@router.delete("/{path:path}", status_code=204)
def revoke_system(
    path: str, backend: BackendDep, settings: SettingsDep, token: AdminTokenDep
) -> Response:
    uri = settings.base_uri + ".system/" + path
    try:
        revoke_token(backend, uri)
    except StorageError as exc:
        raise _http_error(exc) from exc
    return Response(status_code=204)
