"""SPARQL 1.1 Protocol read-only query endpoint over the in-memory graph."""

import re
import urllib.parse
from collections.abc import Mapping
from typing import Annotated

from fastapi import APIRouter, Body, Depends, Header, HTTPException, Query, Request, Response
from pyparsing.exceptions import ParseException
from rdflib.plugins.sparql.sparql import SPARQLError

from ldp_common.apidocs import STORAGE_AUTH, UNAUTHORIZED, Responses, rdf_content
from ldp_common.config import SettingsDep
from ldp_common.rdfcontent import (
    FORMAT_BY_CONTENT_TYPE,
    negotiate_media,
    normalize_media_type,
)
from ldp_personal_store.auth.deps import get_storage_token
from ldp_personal_store.ldp.deps import BackendDep
from ldp_personal_store.storage.backend import StorageBackend

router = APIRouter(prefix="/sparql", tags=["sparql"], dependencies=[Depends(get_storage_token)])

_QUERY_DESCRIPTION = """\
Evaluate a read-only SPARQL 1.1 query over the pod's RDF data (every stored \
resource, as one union graph).

* `SELECT` / `ASK` results negotiate via `Accept` among `application/sparql-results+json` \
(default), `application/sparql-results+xml`, and `text/csv` (`SELECT` only).
* `CONSTRUCT` / `DESCRIBE` results negotiate among the four RDF serializations \
(default `text/turtle`).
* Protocol extension: a request parameter `binding-<name>` binds the SPARQL variable \
`?<name>` to that value before evaluation — the injection-safe way to parameterize a \
fixed query instead of splicing values into the query text. An optional companion \
`bindingtype-<name>` gives that value an XSD datatype IRI, so it binds as a typed \
term (`"2026-07-06"^^xsd:date`) — required to compare a parameter against typed \
date literals.
* Protocol extension: the reserved `.system/` records (views, tokens, policies, the \
access log) are excluded from evaluation by default, so view CONSTRUCTs never see \
them; `include-system=true` widens the scope to the full dataset.
* SPARQL Update is not supported anywhere on this server.
"""

_QUERY_RESPONSES: Responses = {
    200: {
        "description": "The query result in the negotiated format.",
        "content": {
            "application/sparql-results+json": {"schema": {"type": "object"}},
            "application/sparql-results+xml": {"schema": {"type": "string"}},
            "text/csv": {"schema": {"type": "string"}},
            **rdf_content(),
        },
    },
    400: {"description": "Missing query, invalid SPARQL syntax, or an evaluation error."},
    401: UNAUTHORIZED,
    406: {"description": "`text/csv` was requested for a non-`SELECT` result."},
}

_BINDING_PREFIX = "binding-"

_BINDINGTYPE_PREFIX = "bindingtype-"

_INCLUDE_SYSTEM_PARAM = "include-system"

_RESULTS_DEFAULT = "application/sparql-results+json"
_RESULTS_FORMATS: dict[str, str] = {
    "application/sparql-results+json": "json",
    "application/sparql-results+xml": "xml",
    "text/csv": "csv",
}

# CONSTRUCT/DESCRIBE results serialize in the same RDF syntaxes the LDP layer serves.
_RDF_DEFAULT = "text/turtle"


def _extract_bindings(params: Mapping[str, str]) -> dict[str, str] | None:
    # ``bindingtype-<name>`` fields carry datatypes, not values; they do not start
    # with ``binding-`` (the next char is ``t``, not ``-``), so this filter skips them.
    bindings = {
        key.removeprefix(_BINDING_PREFIX): value
        for key, value in params.items()
        if key.startswith(_BINDING_PREFIX)
    }
    return bindings or None


def _extract_binding_types(params: Mapping[str, str]) -> dict[str, str] | None:
    types = {
        key.removeprefix(_BINDINGTYPE_PREFIX): value
        for key, value in params.items()
        if key.startswith(_BINDINGTYPE_PREFIX)
    }
    return types or None


def _extract_include_system(params: Mapping[str, str]) -> bool:
    return params.get(_INCLUDE_SYSTEM_PARAM) == "true"


def _apply_state_scope(sparql: str, state_graph: str) -> tuple[str, bool]:
    """Resolve a standard ``FROM <state-graph>`` dataset clause to full-dataset scope.

    The portable equivalent of the ``include-system`` extension: the engine names the
    reserved state graph in a standard SPARQL ``FROM`` instead of setting a proprietary
    flag. When present, the clause is stripped (this store realizes the state graph as its
    ``.system/`` subtree rather than a literal named graph) and evaluation widens to
    include those records.
    """
    pattern = re.compile(r"\bFROM\s+(?:NAMED\s+)?<" + re.escape(state_graph) + r">", re.IGNORECASE)
    if pattern.search(sparql):
        return pattern.sub(" ", sparql), True
    return sparql, False


def _decode_utf8(body: bytes) -> str:
    try:
        return body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HTTPException(status_code=400, detail=f"Body is not valid UTF-8: {exc}") from exc


def _run_query(
    backend: StorageBackend,
    sparql: str,
    accept: str | None,
    init_bindings: dict[str, str] | None = None,
    include_system: bool = False,
    init_binding_types: dict[str, str] | None = None,
    state_graph: str | None = None,
) -> Response:
    if state_graph is not None:
        sparql, from_state = _apply_state_scope(sparql, state_graph)
        include_system = include_system or from_state
    if not sparql or not sparql.strip():
        raise HTTPException(status_code=400, detail="Missing query")
    # rdflib parses eagerly but evaluates lazily, so a ParseException surfaces at
    # query() while a SPARQLError may not surface until serialization iterates the
    # result — both are faults in the client's query, hence the shared 400 guard.
    try:
        result = backend.query(
            sparql,
            init_bindings=init_bindings,
            include_system=include_system,
            init_binding_types=init_binding_types,
        )
        if result.type in ("SELECT", "ASK"):
            media_type, fmt = negotiate_media(accept, _RESULTS_FORMATS, _RESULTS_DEFAULT)
            if result.type == "ASK" and fmt == "csv":
                # rdflib's CSV serializer rejects non-SELECT results with a bare
                # Exception; refuse the combination before it becomes a 500.
                raise HTTPException(status_code=406, detail="text/csv supports only SELECT results")
            data = result.serialize(format=fmt)
            assert data is not None
        else:
            media_type, fmt = negotiate_media(accept, FORMAT_BY_CONTENT_TYPE, _RDF_DEFAULT)
            assert result.graph is not None
            data = result.graph.serialize(format=fmt, encoding="utf-8")
    except ParseException as exc:
        raise HTTPException(status_code=400, detail=f"Invalid SPARQL query: {exc}") from exc
    except SPARQLError as exc:
        raise HTTPException(status_code=400, detail=f"SPARQL evaluation error: {exc}") from exc
    return Response(content=data, media_type=media_type)


@router.get(
    "",
    operation_id="sparqlQueryGet",
    summary="Run a SPARQL query (query string)",
    description=_QUERY_DESCRIPTION,
    response_class=Response,
    responses=_QUERY_RESPONSES,
    openapi_extra={"security": STORAGE_AUTH},
)
def query_get(
    request: Request,
    backend: BackendDep,
    settings: SettingsDep,
    query: Annotated[
        str | None,
        Query(description="The SPARQL query text (required; its absence is a 400)."),
    ] = None,
    accept: Annotated[str | None, Header()] = None,
) -> Response:
    return _run_query(
        backend,
        query or "",
        accept,
        init_bindings=_extract_bindings(request.query_params),
        include_system=_extract_include_system(request.query_params),
        init_binding_types=_extract_binding_types(request.query_params),
        state_graph=settings.state_graph,
    )


@router.post(
    "",
    operation_id="sparqlQueryPost",
    summary="Run a SPARQL query (request body)",
    description=_QUERY_DESCRIPTION,
    response_class=Response,
    responses={
        **_QUERY_RESPONSES,
        405: {"description": "`application/sparql-update` bodies are rejected."},
        415: {"description": "Body must be `application/sparql-query` or a form."},
    },
    openapi_extra={
        "security": STORAGE_AUTH,
        "requestBody": {
            "required": True,
            "description": (
                "Either the bare query (`application/sparql-query`) or a form whose "
                "`query` field carries it; `binding-<name>` form fields or query-string "
                "parameters bind SPARQL variables, and `include-system=true` widens the "
                "scope to the reserved `.system/` records."
            ),
            "content": {
                "application/sparql-query": {
                    "schema": {"type": "string"},
                    "example": "SELECT ?s ?p ?o WHERE { ?s ?p ?o } LIMIT 10",
                },
                "application/x-www-form-urlencoded": {
                    "schema": {
                        "type": "object",
                        "properties": {"query": {"type": "string"}},
                        "required": ["query"],
                        "additionalProperties": {"type": "string"},
                    },
                },
            },
        },
    },
)
def query_post(
    request: Request,
    backend: BackendDep,
    settings: SettingsDep,
    body: Annotated[bytes, Body()],
    content_type: Annotated[str | None, Header()] = None,
    accept: Annotated[str | None, Header()] = None,
) -> Response:
    normalized_ct = normalize_media_type(content_type)
    if normalized_ct == "application/sparql-update":
        raise HTTPException(status_code=405)
    if normalized_ct == "application/sparql-query":
        sparql = _decode_utf8(body)
        bindings = _extract_bindings(request.query_params)
        binding_types = _extract_binding_types(request.query_params)
        include_system = _extract_include_system(request.query_params)
    elif normalized_ct == "application/x-www-form-urlencoded":
        fields = {
            key: values[0] for key, values in urllib.parse.parse_qs(_decode_utf8(body)).items()
        }
        sparql = fields.get("query", "")
        bindings = _extract_bindings(fields) or _extract_bindings(request.query_params)
        binding_types = _extract_binding_types(fields) or _extract_binding_types(
            request.query_params
        )
        include_system = _extract_include_system(fields) or _extract_include_system(
            request.query_params
        )
    else:
        raise HTTPException(status_code=415)
    return _run_query(
        backend,
        sparql,
        accept,
        init_bindings=bindings,
        include_system=include_system,
        init_binding_types=binding_types,
        state_graph=settings.state_graph,
    )
