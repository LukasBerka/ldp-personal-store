"""FastAPI bearer-token dependencies for the storage server's HTTP surface.

The storage surface accepts only administrative credentials, in the sense of the
``.system/`` prefix invariant: the pod owner's admin token, which authorizes the
full management surface, and the view engine's token, which participates in the
request path with read access plus the enforcement-field writes. Routers attach
the Annotated aliases so a handler receives an already-validated
:class:`TokenRecord`, or the request is rejected with an identical 401 before the
handler runs.
"""

from typing import Annotated

from fastapi import Depends, Header, HTTPException

from app.auth.tokens import TokenRecord, validate_token, validate_token_one_of
from app.ldp.deps import BackendDep
from app.vocab import POD_AdminToken, POD_EngineToken


def require_bearer(authorization: Annotated[str | None, Header()] = None) -> str:
    if authorization is None or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, headers={"WWW-Authenticate": "Bearer"})
    return authorization.removeprefix("Bearer ")


def get_admin_token(
    raw: Annotated[str, Depends(require_bearer)],
    backend: BackendDep,
) -> TokenRecord:
    return validate_token(backend, raw, POD_AdminToken)


def get_storage_token(
    raw: Annotated[str, Depends(require_bearer)],
    backend: BackendDep,
) -> TokenRecord:
    return validate_token_one_of(backend, raw, (POD_AdminToken, POD_EngineToken))


AdminTokenDep = Annotated[TokenRecord, Depends(get_admin_token)]
StorageTokenDep = Annotated[TokenRecord, Depends(get_storage_token)]
