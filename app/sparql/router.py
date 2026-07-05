"""SPARQL 1.1 Protocol read-only query endpoint over the in-memory graph.

This endpoint is the query half of the storage HTTP surface the view engine talks
to: SPARQL has no per-query URI scope, so a valid administrative credential (the
pod owner's admin token or the engine's token) is required for every query.

Extension parameters: any ``binding-<name>`` request parameter is bound as the
SPARQL variable ``?<name>`` via rdflib ``initBindings`` before evaluation — the
same injection-safe mechanism the engine used in-process, now carried over the
protocol. Standard SPARQL Protocol clients that send no such parameters are
unaffected.

Handlers are synchronous: the backend performs blocking rdflib and lock work, and
FastAPI runs sync path operations in a threadpool, which is the correct execution
model for blocking code.
"""

import urllib.parse
from collections.abc import Mapping
from typing import Annotated

from fastapi import APIRouter, Body, Depends, Header, HTTPException, Query, Request, Response
from pyparsing.exceptions import ParseException

from app.auth.deps import get_storage_token
from app.ldp.deps import BackendDep
from app.storage.backend import StorageBackend

router = APIRouter(prefix="/sparql", tags=["sparql"], dependencies=[Depends(get_storage_token)])

_BINDING_PREFIX = "binding-"

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
    "application/rdf+xml": "xml",
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


def _extract_bindings(params: Mapping[str, str]) -> dict[str, str] | None:
    bindings = {
        key.removeprefix(_BINDING_PREFIX): value
        for key, value in params.items()
        if key.startswith(_BINDING_PREFIX)
    }
    return bindings or None


def _run_query(
    backend: StorageBackend,
    sparql: str,
    accept: str | None,
    init_bindings: dict[str, str] | None = None,
) -> Response:
    if not sparql or not sparql.strip():
        raise HTTPException(status_code=400, detail="Missing query")
    try:
        result = backend.query(sparql, init_bindings=init_bindings)
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
    request: Request,
    backend: BackendDep,
    query: Annotated[str | None, Query()] = None,
    accept: Annotated[str | None, Header()] = None,
) -> Response:
    return _run_query(
        backend, query or "", accept, init_bindings=_extract_bindings(request.query_params)
    )


@router.post("")
def query_post(
    request: Request,
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
        bindings = _extract_bindings(request.query_params)
    elif normalized_ct == "application/x-www-form-urlencoded":
        fields = {
            key: values[0] for key, values in urllib.parse.parse_qs(body.decode("utf-8")).items()
        }
        sparql = fields.get("query", "")
        bindings = _extract_bindings(fields) or _extract_bindings(request.query_params)
    else:
        raise HTTPException(status_code=415)
    return _run_query(backend, sparql, accept, init_bindings=bindings)
