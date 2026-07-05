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


def _submission_or_422(body: bytes, content_type: str | None) -> ViewSubmission:
    graph = parse_rdf_body(body, content_type)
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


@router.post("", status_code=201)
def create_view(
    request: Request,
    backend: BackendDep,
    body: RawBodyDep,
    content_type: Annotated[str | None, Header()] = None,
    slug: Annotated[str | None, Header()] = None,
) -> Response:
    submission = _submission_or_422(body, content_type)
    view_uri = str(request.app.state.system_ns) + "views/" + _mint_view_id(slug)
    response = _store(backend, view_uri, submission)
    response.status_code = 201
    return response


@router.put("/{view_id}")
def replace_view(
    view_id: str,
    request: Request,
    backend: BackendDep,
    body: RawBodyDep,
    content_type: Annotated[str | None, Header()] = None,
) -> Response:
    submission = _submission_or_422(body, content_type)
    view_uri = str(request.app.state.system_ns) + "views/" + view_id
    try:
        backend.read(view_uri)
        exists = True
    except ResourceNotFound:
        exists = False
    response = _store(backend, view_uri, submission)
    response.status_code = 200 if exists else 201
    return response


@router.delete("/{view_id}", status_code=204)
def delete_view(view_id: str, request: Request, backend: BackendDep) -> Response:
    view_uri = str(request.app.state.system_ns) + "views/" + view_id
    try:
        backend.delete_system(view_uri)
    except ResourceNotFound as exc:
        raise HTTPException(status_code=404) from exc
    return Response(status_code=204)
