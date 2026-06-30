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
from rdflib import BNode, Graph, URIRef
from rdflib import Literal as RDFLiteral
from rdflib.namespace import RDF, XSD
from rdflib.plugins.sparql.parser import parseQuery
from rdflib.term import Node

from app.vocab import (
    DC_description,
    DC_title,
    POD_constructTemplate,
    POD_contentTypeHint,
    POD_parameter,
    POD_paramName,
    POD_paramType,
    POD_View,
)

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


def _value_str(graph: Graph, subject: Node, predicate: URIRef) -> str:
    value = graph.value(subject, predicate)
    return str(value) if value is not None else ""


def to_view_graph(
    uri: str,
    title: str,
    description: str,
    template: str,
    ct_hint: str,
    params: list[ParamDecl],
) -> Graph:
    """Serialize a view definition into an rdflib Graph ready for ``write_system``.

    Each parameter becomes a fresh blank node; blank-node labels are never
    referenced externally, so their instability across a serialize/parse cycle is
    irrelevant (``parse_view_record`` recovers them by predicate traversal).
    """
    graph = Graph()
    subject = URIRef(uri)
    graph.add((subject, RDF.type, POD_View))
    graph.add((subject, DC_title, RDFLiteral(title, datatype=XSD.string)))
    graph.add((subject, DC_description, RDFLiteral(description, datatype=XSD.string)))
    graph.add((subject, POD_constructTemplate, RDFLiteral(template, datatype=XSD.string)))
    graph.add((subject, POD_contentTypeHint, RDFLiteral(ct_hint, datatype=XSD.string)))
    for param in params:
        pnode = BNode()
        graph.add((subject, POD_parameter, pnode))
        graph.add((pnode, POD_paramName, RDFLiteral(param.name, datatype=XSD.string)))
        graph.add((pnode, POD_paramType, RDFLiteral(param.type, datatype=XSD.string)))
    return graph


def parse_view_record(graph: Graph, uri: str) -> ViewRecord:
    """Reconstruct a ViewRecord from its stored RDF triples.

    Parameters are gathered by traversing ``POD_parameter`` predicates rather than
    by blank-node identity, so the record survives a Turtle serialize/parse cycle.
    """
    subject = URIRef(uri)
    params: list[ParamDecl] = []
    for pnode in graph.objects(subject, POD_parameter):
        params.append(
            ParamDecl.model_validate(
                {
                    "name": _value_str(graph, pnode, POD_paramName),
                    "type": _value_str(graph, pnode, POD_paramType),
                }
            )
        )
    return ViewRecord(
        uri=uri,
        title=_value_str(graph, subject, DC_title),
        description=_value_str(graph, subject, DC_description),
        construct_template=_value_str(graph, subject, POD_constructTemplate),
        content_type_hint=_value_str(graph, subject, POD_contentTypeHint),
        params=params,
    )
