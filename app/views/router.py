"""Admin-gated management router for stored view definitions.

POST/PUT/DELETE under ``/.system/views`` mint, replace, and remove view records
through the storage backend's system-write path, so a new or changed definition is
live in the in-memory graph immediately with no restart. Definition-time validation
rejects non-CONSTRUCT templates and param/template mismatches with 422. This router
is mounted ahead of the ``.system/`` catch-all so these operations win route
resolution while a GET of a view still falls through to the system Turtle reader.
"""

import secrets
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from pydantic import BaseModel

from app.auth.deps import get_admin_token
from app.ldp.containers import _sanitize_slug
from app.ldp.deps import BackendDep
from app.storage.backend import ResourceNotFound, StorageBackend
from app.views.model import (
    ParamDecl,
    check_params_against_template,
    to_view_graph,
    validate_construct_template,
)

router = APIRouter(prefix="/.system/views", tags=["views"], dependencies=[Depends(get_admin_token)])


class ParamDeclRequest(BaseModel):
    name: str
    type: Literal["str", "int", "iri"]


class ViewCreateRequest(BaseModel):
    title: str
    description: str = ""
    construct_template: str
    content_type_hint: Literal["text/turtle", "application/ld+json", "application/n-triples"] = (
        "text/turtle"
    )
    params: list[ParamDeclRequest] = []
    max_view_retrievals: int | None = None


class ViewCreateResponse(BaseModel):
    view_uri: str


def _validate_or_422(template: str, decls: list[ParamDecl]) -> None:
    try:
        validate_construct_template(template)
        check_params_against_template(template, decls)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _mint_view_id(slug: str | None) -> str:
    if slug:
        sanitized = _sanitize_slug(slug)
        if sanitized:
            return sanitized
    return secrets.token_urlsafe(8)


def _build_and_store(backend: StorageBackend, view_uri: str, body: ViewCreateRequest) -> None:
    decls = [ParamDecl(name=p.name, type=p.type) for p in body.params]
    _validate_or_422(body.construct_template, decls)
    graph = to_view_graph(
        view_uri,
        body.title,
        body.description,
        body.construct_template,
        body.content_type_hint,
        decls,
        max_view_retrievals=body.max_view_retrievals,
    )
    backend.write_system(view_uri, graph)


@router.post("", status_code=201)
def create_view(
    request: Request,
    backend: BackendDep,
    body: ViewCreateRequest,
    response: Response,
    slug: Annotated[str | None, Header()] = None,
) -> ViewCreateResponse:
    view_id = _mint_view_id(slug)
    view_uri = str(request.app.state.system_ns) + "views/" + view_id
    _build_and_store(backend, view_uri, body)
    response.headers["Location"] = view_uri
    return ViewCreateResponse(view_uri=view_uri)


@router.put("/{view_id}")
def replace_view(
    view_id: str,
    request: Request,
    backend: BackendDep,
    body: ViewCreateRequest,
) -> ViewCreateResponse:
    view_uri = str(request.app.state.system_ns) + "views/" + view_id
    _build_and_store(backend, view_uri, body)
    return ViewCreateResponse(view_uri=view_uri)


@router.delete("/{view_id}", status_code=204)
def delete_view(view_id: str, request: Request, backend: BackendDep) -> Response:
    view_uri = str(request.app.state.system_ns) + "views/" + view_id
    try:
        backend.delete_system(view_uri)
    except ResourceNotFound as exc:
        raise HTTPException(status_code=404) from exc
    return Response(status_code=204)
