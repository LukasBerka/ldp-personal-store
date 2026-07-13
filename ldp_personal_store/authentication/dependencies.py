"""FastAPI bearer-token dependencies for the storage server's HTTP surface."""

from typing import Annotated

from fastapi import Depends

from ldp_common.http import require_bearer
from ldp_common.tokenrecord import TokenRecord
from ldp_common.vocabulary import POD_AdminToken, POD_EngineToken
from ldp_personal_store.authentication.tokens_store import validate_token, validate_token_one_of
from ldp_personal_store.ldp.dependencies import BackendDep

__all__ = [
    "AdminTokenDep",
    "StorageTokenDep",
    "get_admin_token",
    "get_storage_token",
    "require_bearer",
]


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
