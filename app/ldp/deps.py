"""FastAPI dependency exposing the per-process storage backend.

The backend is constructed once in the app lifespan and stored on
``app.state``; route handlers receive it through :data:`BackendDep` rather than
reaching into application state directly.
"""

from typing import Annotated

from fastapi import Depends, Request

from app.storage.backend import StorageBackend


def get_backend(request: Request) -> StorageBackend:
    return request.app.state.backend


BackendDep = Annotated[StorageBackend, Depends(get_backend)]
