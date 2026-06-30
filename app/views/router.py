"""Admin-gated management router for stored view definitions.

POST/PUT/DELETE under ``/.system/views`` mint, replace, and remove view records
through the storage backend's system-write path, so a new or changed definition is
live in the in-memory graph immediately with no restart. Definition-time validation
rejects non-CONSTRUCT templates and param/template mismatches with 422. This router
is mounted ahead of the ``.system/`` catch-all so these operations win route
resolution while a GET of a view still falls through to the system Turtle reader.
"""

import secrets
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.auth.deps import get_admin_token
from app.ldp.containers import _sanitize_slug
from app.views.model import (
    ParamDecl,
    check_params_against_template,
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
