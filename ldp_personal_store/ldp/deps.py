"""FastAPI dependencies and helpers shared by the storage server's HTTP routers.

The backend is constructed once in the app lifespan and stored on
``app.state``; route handlers receive it through :data:`BackendDep` rather than
reaching into application state directly. :func:`http_error` is the single
storage-exception-to-HTTP translation every router uses.
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
    """The request body as raw bytes, whatever its Content-Type.

    A ``bytes``-typed Body() parameter is JSON-decoded first when the request
    carries a JSON Content-Type, which turns an unsupported-media-type request
    into a 422 before the handler can answer 415; this dependency sidesteps that.
    """
    return await request.body()


BackendDep = Annotated[StorageBackend, Depends(get_backend)]
RawBodyDep = Annotated[bytes, Depends(get_raw_body)]
