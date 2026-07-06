"""Admin-gated management router for stored view definitions.

Views are LDP resources managed with the same verbs and representations as the
pod owner's data: POST an RDF representation to ``/.system/views`` to create
(``Slug`` honored), PUT one to ``/.system/views/{view_id}`` to replace, DELETE
to remove; a GET of a view falls through to the system Turtle reader, and a GET
of the ``/.system/views/`` container lists the catalog. A new or changed
definition is live in the in-memory graph immediately with no restart.
Definition-time validation rejects non-CONSTRUCT templates, param/template
mismatches, and unsupported content-type hints with 422. This router is mounted
ahead of the ``.system/`` catch-all so these operations win route resolution.
"""

import secrets
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response

from app.apidocs import ADMIN_AUTH, UNAUTHORIZED, rdf_request_body, turtle_response
from app.auth.deps import get_admin_token
from app.ldp.containers import sanitize_slug
from app.ldp.content import RDF_CONTENT_TYPES, parse_rdf_body
from app.ldp.deps import BackendDep, RawBodyDep
from app.storage.backend import ResourceNotFound, StorageBackend
from app.views.model import (
    ViewSubmission,
    check_params_against_template,
    parse_view_submission,
    to_view_graph,
    validate_construct_template,
)

router = APIRouter(prefix="/.system/views", tags=["views"], dependencies=[Depends(get_admin_token)])

_VIEW_BODY = rdf_request_body(
    "Exactly one subject typed `pod:View` (the subject term is irrelevant) with: "
    "`dcterms:title` (required); `dcterms:description`; `pod:constructTemplate` — a SPARQL "
    "CONSTRUCT query, the sole filter on what the view exposes (required); "
    "`pod:contentTypeHint` — the result serialization, one of the four RDF media types "
    "(default `text/turtle`); `pod:maxViewRetrievals` — a delivery ceiling shared across "
    "all grants on this view; and one `pod:parameter` blank node per consumer-suppliable "
    "parameter, carrying `pod:paramName` and `pod:paramType` (`str`, `int`, or `iri`). "
    "Every declared parameter must appear as `?name` in the template; values are bound as "
    "RDF terms, never spliced into the query text. An `int` value still binds as a plain "
    "literal, so templates should coerce explicitly (e.g. `FILTER(xsd:integer(?n) > 5)`).",
    "@prefix pod: <urn:pod:vocab:> .\n"
    "@prefix dcterms: <http://purl.org/dc/terms/> .\n\n"
    "[] a pod:View ;\n"
    '    dcterms:title "Reading list" ;\n'
    '    dcterms:description "Public books, filtered by author" ;\n'
    '    pod:constructTemplate """PREFIX schema: <http://schema.org/>\n'
    "CONSTRUCT { ?book schema:name ?title }\n"
    'WHERE { ?book a schema:Book ; schema:author ?author ; schema:name ?title }""" ;\n'
    '    pod:contentTypeHint "text/turtle" ;\n'
    '    pod:maxViewRetrievals "100"^^<http://www.w3.org/2001/XMLSchema#integer> ;\n'
    '    pod:parameter [ pod:paramName "author" ; pod:paramType "str" ] .',
)

_VIEW_422 = {
    "description": (
        "Rejected definition: not exactly one `pod:View` subject, a missing title or "
        "template, a template that is not syntactically valid CONSTRUCT, a declared "
        "parameter absent from the template, an ill-typed parameter, or an unsupported "
        "content-type hint. The `detail` names the problem."
    )
}


def _submission_or_422(body: bytes, content_type: str | None, base_uri: str) -> ViewSubmission:
    graph = parse_rdf_body(body, content_type, base_uri=base_uri)
    try:
        submission = parse_view_submission(graph)
        validate_construct_template(submission.construct_template)
        check_params_against_template(submission.construct_template, submission.params)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if submission.content_type_hint not in RDF_CONTENT_TYPES:
        raise HTTPException(
            status_code=422,
            detail=f"content-type hint must be one of {sorted(RDF_CONTENT_TYPES)}",
        )
    return submission


def _mint_view_id(slug: str | None) -> str:
    if slug:
        sanitized = sanitize_slug(slug)
        if sanitized:
            return sanitized
    return secrets.token_urlsafe(8)


def _store(backend: StorageBackend, view_uri: str, submission: ViewSubmission) -> Response:
    stored = to_view_graph(
        view_uri,
        submission.title,
        submission.description,
        submission.construct_template,
        submission.content_type_hint,
        submission.params,
        max_view_retrievals=submission.max_view_retrievals,
    )
    backend.write_system(view_uri, stored)
    return Response(
        content=stored.serialize(format="turtle"),
        media_type="text/turtle",
        headers={"Location": view_uri},
    )


@router.post(
    "",
    status_code=201,
    operation_id="createView",
    summary="Create a view definition",
    description=(
        "Define a named, parameterized SPARQL CONSTRUCT view. The view id is minted from "
        "the `Slug` header when possible, randomly otherwise; the definition is live for "
        "consumers immediately. Consumers fetch results at `/.engine/views/{view_id}` "
        "once a grant links the view."
    ),
    response_class=Response,
    responses={
        201: turtle_response("The stored definition; `Location` names it."),
        400: {"description": "The RDF body does not parse."},
        401: UNAUTHORIZED,
        415: {"description": "`Content-Type` is not one of the four RDF media types."},
        422: _VIEW_422,
    },
    openapi_extra={"security": ADMIN_AUTH, "requestBody": _VIEW_BODY},
)
def create_view(
    request: Request,
    backend: BackendDep,
    body: RawBodyDep,
    content_type: Annotated[str | None, Header()] = None,
    slug: Annotated[str | None, Header()] = None,
) -> Response:
    views_ns = str(request.app.state.system_ns) + "views/"
    submission = _submission_or_422(body, content_type, views_ns)
    view_uri = views_ns + _mint_view_id(slug)
    response = _store(backend, view_uri, submission)
    response.status_code = 201
    return response


@router.put(
    "/{view_id}",
    operation_id="replaceView",
    summary="Create or replace a view definition at a chosen id",
    description=(
        "Full replace (no merge) of the definition at this id, creating it when absent. "
        "The stored shape is exactly what a `GET /.system/views/{view_id}` returns, so a "
        "GET-edit-PUT roundtrip works with any client."
    ),
    response_class=Response,
    responses={
        200: turtle_response("Replaced; the stored definition."),
        201: turtle_response("Created; the stored definition."),
        400: {"description": "The RDF body does not parse."},
        401: UNAUTHORIZED,
        415: {"description": "`Content-Type` is not one of the four RDF media types."},
        422: _VIEW_422,
    },
    openapi_extra={"security": ADMIN_AUTH, "requestBody": _VIEW_BODY},
)
def replace_view(
    view_id: str,
    request: Request,
    backend: BackendDep,
    body: RawBodyDep,
    content_type: Annotated[str | None, Header()] = None,
) -> Response:
    view_uri = str(request.app.state.system_ns) + "views/" + view_id
    submission = _submission_or_422(body, content_type, view_uri)
    try:
        backend.read(view_uri)
        exists = True
    except ResourceNotFound:
        exists = False
    response = _store(backend, view_uri, submission)
    response.status_code = 200 if exists else 201
    return response


@router.delete(
    "/{view_id}",
    status_code=204,
    operation_id="deleteView",
    summary="Delete a view definition",
    description=(
        "Remove the definition; consumers lose access immediately. Grants that still "
        "link the view keep working for their other views and list the stale member "
        "in discovery without metadata until the owner reissues or revokes them."
    ),
    response_class=Response,
    responses={
        204: {"description": "Deleted."},
        401: UNAUTHORIZED,
        404: {"description": "No view at this id."},
    },
    openapi_extra={"security": ADMIN_AUTH},
)
def delete_view(view_id: str, request: Request, backend: BackendDep) -> Response:
    view_uri = str(request.app.state.system_ns) + "views/" + view_id
    try:
        backend.delete_system(view_uri)
    except ResourceNotFound as exc:
        raise HTTPException(status_code=404) from exc
    return Response(status_code=204)
