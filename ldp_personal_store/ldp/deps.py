"""FastAPI dependencies and helpers shared by the storage server's HTTP routers.
"""

from typing import Annotated

from fastapi import Depends, HTTPException, Request

from ldp_personal_store.storage.backend import (
    NotABinaryResource,
    PrefixViolation,
    ResourceNotFound,
    StorageBackend,
    StorageError,
)


def http_error(exc: StorageError) -> HTTPException:
    """Translate a storage-layer exception into its HTTP equivalent."""
    if isinstance(exc, ResourceNotFound):
        return HTTPException(status_code=404)
    if isinstance(exc, PrefixViolation):
        return HTTPException(status_code=403)
    if isinstance(exc, NotABinaryResource):
        return HTTPException(status_code=409)
    return HTTPException(status_code=500)


def get_backend(request: Request) -> StorageBackend:
    return request.app.state.backend


async def get_raw_body(request: Request) -> bytes:
    return await request.body()


BackendDep = Annotated[StorageBackend, Depends(get_backend)]
RawBodyDep = Annotated[bytes, Depends(get_raw_body)]
