"""Framework-free view-definition model: typed records, RDF (de)serialization,
CONSTRUCT-template validation, and injection-safe parameter binding.

This module imports only rdflib and Pydantic — never FastAPI or any HTTP layer —
so the view engine can depend on it without pulling in the router.

Integer-binding caveat: a declared ``"int"`` parameter is still bound as a plain
string term (``_to_term`` in the storage backend turns a scheme-less value into a
plain ``Literal`` with no ``xsd:integer`` datatype). A template that compares
against a bare integer literal (``FILTER(?count = 42)``) will not match, because
``Literal("42") != Literal(42, datatype=xsd:integer)``. Templates must therefore
coerce explicitly, e.g. ``FILTER(xsd:integer(?n) = 42)`` or ``FILTER(str(?count) =
str(?n))``.
"""

import re
from typing import Literal

from pydantic import BaseModel
from pyparsing.exceptions import ParseException
from rdflib.plugins.sparql.parser import parseQuery

ParamTypeName = Literal["str", "int", "iri"]


class ParamDecl(BaseModel):
    name: str
    type: ParamTypeName


class ViewRecord(BaseModel):
    uri: str
    title: str
    description: str
    construct_template: str
    content_type_hint: str
    params: list[ParamDecl]


def validate_construct_template(template: str) -> None:
    """Raise ValueError unless *template* is syntactically valid SPARQL CONSTRUCT.

    Malformed SPARQL surfaces as a pyparsing ParseException; a well-formed but
    non-CONSTRUCT query (SELECT/ASK/DESCRIBE) is identified by the parsed query
    object's ``name`` and rejected. Callers translate the ValueError into 422.
    """
    try:
        parsed = parseQuery(template)
    except ParseException as exc:
        raise ValueError(f"Invalid SPARQL syntax: {exc}") from exc
    # parsed[1] is rdflib's dynamic CompValue; its query-type marker lives on .name.
    query_type = getattr(parsed[1], "name", None)
    if query_type != "ConstructQuery":
        raise ValueError(f"Template must be a CONSTRUCT query, got {query_type!r}")


def check_params_against_template(template: str, decls: list[ParamDecl]) -> None:
    """Raise ValueError if a declared parameter never appears as ``?name`` in the
    template. The regex scan may false-positive on a ``?name`` inside a string
    literal, which is acceptable for a personal pod's view authoring workflow.
    """
    template_vars = set(re.findall(r"\?(\w+)", template))
    for decl in decls:
        if decl.name not in template_vars:
            raise ValueError(f"Declared parameter {decl.name!r} does not appear in the template")
