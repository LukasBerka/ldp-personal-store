"""Shared HTTP helpers: the bearer-header check both roles' auth dependencies build on."""

from typing import Annotated

from fastapi import Header, HTTPException


def require_bearer(authorization: Annotated[str | None, Header()] = None) -> str:
    if authorization is None or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, headers={"WWW-Authenticate": "Bearer"})
    return authorization.removeprefix("Bearer ")
