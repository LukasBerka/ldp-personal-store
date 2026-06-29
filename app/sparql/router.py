"""SPARQL 1.1 Protocol read-only query endpoint over the in-memory graph.

Handlers are synchronous: the backend performs blocking rdflib and lock work, and
FastAPI runs sync path operations in a threadpool, which is the correct execution
model for blocking code.
"""

import urllib.parse
from typing import Annotated

from fastapi import APIRouter, Body, Depends, Header, HTTPException, Query, Response
from pyparsing.exceptions import ParseException

from app.auth.deps import get_admin_token
from app.ldp.deps import BackendDep
from app.storage.backend import StorageBackend

# The whole endpoint is the engine->storage boundary: SPARQL has no per-query URI
# scope, so a valid admin token is required for every query.
router = APIRouter(prefix="/sparql", tags=["sparql"], dependencies=[Depends(get_admin_token)])

_RESULTS_DEFAULT = "application/sparql-results+json"
_RESULTS_FORMATS: dict[str, str] = {
    "application/sparql-results+json": "json",
    "application/sparql-results+xml": "xml",
    "text/csv": "csv",
}

_RDF_DEFAULT = "text/turtle"
_RDF_FORMATS: dict[str, str] = {
    "text/turtle": "turtle",
    "application/ld+json": "json-ld",
    "application/n-triples": "nt",
}


def _negotiate(
    accept: str | None, formats: dict[str, str], default_media: str
) -> tuple[str, str]:
    if not accept or "*/*" in accept:
        return default_media, formats[default_media]
    for entry in accept.split(","):
        media_type = entry.split(";")[0].strip().lower()
        if media_type in formats:
            return media_type, formats[media_type]
    raise HTTPException(status_code=406)


def _run_query(backend: StorageBackend, sparql: str, accept: str | None) -> Response:
    if not sparql or not sparql.strip():
        raise HTTPException(status_code=400, detail="Missing query")
    try:
        result = backend.query(sparql)
    except ParseException as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if result.type in ("SELECT", "ASK"):
        media_type, fmt = _negotiate(accept, _RESULTS_FORMATS, _RESULTS_DEFAULT)
        data = result.serialize(format=fmt)
        assert data is not None
    else:
        media_type, fmt = _negotiate(accept, _RDF_FORMATS, _RDF_DEFAULT)
        assert result.graph is not None
        data = result.graph.serialize(format=fmt, encoding="utf-8")
    return Response(content=data, media_type=media_type)


@router.get("")
def query_get(
    backend: BackendDep,
    query: Annotated[str | None, Query()] = None,
    accept: Annotated[str | None, Header()] = None,
) -> Response:
    return _run_query(backend, query or "", accept)


@router.post("")
def query_post(
    backend: BackendDep,
    body: Annotated[bytes, Body()],
    content_type: Annotated[str | None, Header()] = None,
    accept: Annotated[str | None, Header()] = None,
) -> Response:
    normalized_ct = (content_type or "").split(";")[0].strip().lower()
    if normalized_ct == "application/sparql-update":
        raise HTTPException(status_code=405)
    if normalized_ct == "application/sparql-query":
        sparql = body.decode("utf-8")
    elif normalized_ct == "application/x-www-form-urlencoded":
        sparql = urllib.parse.parse_qs(body.decode("utf-8")).get("query", [""])[0]
    else:
        raise HTTPException(status_code=415)
    return _run_query(backend, sparql, accept)
