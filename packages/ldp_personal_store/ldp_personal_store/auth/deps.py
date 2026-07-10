"""FastAPI bearer-token dependencies for the storage server's HTTP surface."""

from typing import Annotated

from fastapi import Depends, Header, HTTPException

from ldp_personal_store.auth.tokens import TokenRecord, validate_token, validate_token_one_of
from ldp_personal_store.ldp.deps import BackendDep
from ldp_personal_store.vocab import POD_AdminToken, POD_EngineToken


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
