"""Admin-gated management router for the reserved ``.system/`` subtree.

Every route requires a valid admin token via the router-level dependency, and the
router is mounted ahead of the public LDP catch-all so ``.system/`` paths are
adjudicated here — issuing consumer tokens, revoking token records, and serving
administrative reads — instead of reaching the public handlers.
"""

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel

from app.auth.deps import get_admin_token
from app.auth.tokens import mint_token, revoke_token
from app.config import SettingsDep
from app.ldp.deps import BackendDep
from app.storage.backend import ResourceNotFound, StorageError

router = APIRouter(prefix="/.system", tags=["system"], dependencies=[Depends(get_admin_token)])


class TokenIssueRequest(BaseModel):
    linked_view_uri: str | None = None


class TokenIssueResponse(BaseModel):
    # The plaintext is unrecoverable: it is surfaced here exactly once and never
    # persisted — only its SHA-256 hash is stored on the record.
    token: str
    record_uri: str


def _http_error(exc: StorageError) -> HTTPException:
    if isinstance(exc, ResourceNotFound):
        return HTTPException(status_code=404)
    return HTTPException(status_code=500)


@router.post("/tokens")
def issue_token(
    request: Request,
    backend: BackendDep,
    body: TokenIssueRequest,
) -> TokenIssueResponse:
    plaintext, record_uri = mint_token(
        backend, request.app.state.system_ns, body.linked_view_uri
    )
    return TokenIssueResponse(token=plaintext, record_uri=record_uri)


@router.get("/{path:path}")
def read_system(path: str, backend: BackendDep, settings: SettingsDep) -> Response:
    uri = settings.base_uri + ".system/" + path
    try:
        graph = backend.read(uri)
    except StorageError as exc:
        raise _http_error(exc) from exc
    return Response(content=graph.serialize(format="turtle"), media_type="text/turtle")


@router.delete("/{path:path}", status_code=204)
def revoke_system(path: str, backend: BackendDep, settings: SettingsDep) -> Response:
    uri = settings.base_uri + ".system/" + path
    try:
        revoke_token(backend, uri)
    except StorageError as exc:
        raise _http_error(exc) from exc
    return Response(status_code=204)
