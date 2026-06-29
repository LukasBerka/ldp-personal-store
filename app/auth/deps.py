"""FastAPI bearer-token dependencies for the two authentication boundaries.

Both boundaries share one validation mechanism (:func:`validate_token`); the only
difference is the required token type. Routers attach the Annotated aliases so a
handler receives an already-validated :class:`TokenRecord`, or the request is
rejected with an identical 401 before the handler runs.
"""

from typing import Annotated

from fastapi import Depends, Header, HTTPException

from app.auth.tokens import TokenRecord, validate_token
from app.ldp.deps import BackendDep
from app.vocab import POD_AdminToken, POD_ConsumerToken


def _require_bearer(authorization: Annotated[str | None, Header()] = None) -> str:
    if authorization is None or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, headers={"WWW-Authenticate": "Bearer"})
    return authorization.removeprefix("Bearer ")


def get_consumer_token(
    raw: Annotated[str, Depends(_require_bearer)],
    backend: BackendDep,
) -> TokenRecord:
    return validate_token(backend, raw, POD_ConsumerToken)


def get_admin_token(
    raw: Annotated[str, Depends(_require_bearer)],
    backend: BackendDep,
) -> TokenRecord:
    return validate_token(backend, raw, POD_AdminToken)


ConsumerTokenDep = Annotated[TokenRecord, Depends(get_consumer_token)]
AdminTokenDep = Annotated[TokenRecord, Depends(get_admin_token)]
