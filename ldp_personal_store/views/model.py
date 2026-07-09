"""Framework-free view-definition model: typed records, RDF (de)serialization,
CONSTRUCT-template validation, and injection-safe parameter binding.
"""

import re
from typing import Literal

from pydantic import BaseModel, TypeAdapter, ValidationError
from pyparsing.exceptions import ParseException
from rdflib import BNode, Graph, URIRef
from rdflib import Literal as RDFLiteral
from rdflib.namespace import RDF, XSD
from rdflib.plugins.sparql.parser import parseQuery
from rdflib.term import Node

from ldp_personal_store.vocab import (
    DC_description,
    DC_title,
    POD_constructTemplate,
    POD_contentTypeHint,
    POD_maxViewRetrievals,
    POD_parameter,
    POD_paramName,
    POD_paramType,
    POD_View,
    POD_viewRetrievalCount,
)

ParamTypeName = Literal["str", "int", "iri", "date", "dateTime"]

# Declared parameter types that bind as a typed RDF literal rather than a plain one,
# mapped to the XSD datatype the value carries into the query.
_PARAM_DATATYPE: dict[str, URIRef] = {"date": XSD.date, "dateTime": XSD.dateTime}

# Same scheme test the storage backend uses to decide URIRef-vs-Literal coercion, so a
# value bound portably (injected VALUES) is the identical term the initBindings path made.
_TERM_IRI_SCHEME = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*:")


def param_term(value: str, param_type: ParamTypeName) -> URIRef | RDFLiteral:
    """The rdflib term a validated parameter *value* of *param_type* binds as.

    Mirrors the storage backend's term coercion exactly, so injecting the term into a
    query (portable ``VALUES`` binding) reproduces the same RDF term the non-standard
    initBindings path produced: ``date``/``dateTime`` bind as typed literals,
    absolute-IRI-shaped values as ``URIRef``\\ s, and everything else as plain literals.
    """
    datatype = _PARAM_DATATYPE.get(param_type)
    if datatype is not None:
        return RDFLiteral(value, datatype=datatype)
    if _TERM_IRI_SCHEME.match(value):
        return URIRef(value)
    return RDFLiteral(value)


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


class ViewSubmission(BaseModel):
    """A view definition as extracted from a client-submitted RDF representation."""

    title: str
    description: str
    construct_template: str
    content_type_hint: str
    params: list[ParamDecl]
    max_view_retrievals: int | None


def parse_view_submission(graph: Graph) -> ViewSubmission:
    """Extract a view definition from a client-submitted RDF graph."""
    subjects = list(graph.subjects(RDF.type, POD_View))
    if len(subjects) != 1:
        raise ValueError("Body must describe exactly one pod:View resource")
    subject = subjects[0]

    title = graph.value(subject, DC_title)
    if title is None:
        raise ValueError("Missing dcterms:title")
    template = graph.value(subject, POD_constructTemplate)
    if template is None:
        raise ValueError("Missing pod:constructTemplate")
    hint = graph.value(subject, POD_contentTypeHint)

    params: list[ParamDecl] = []
    for pnode in graph.objects(subject, POD_parameter):
        try:
            params.append(
                ParamDecl.model_validate(
                    {
                        "name": _value_str(graph, pnode, POD_paramName),
                        "type": _value_str(graph, pnode, POD_paramType),
                    }
                )
            )
        except ValidationError as exc:
            raise ValueError(f"Invalid parameter declaration: {exc}") from exc

    ceiling = graph.value(subject, POD_maxViewRetrievals)
    try:
        max_view_retrievals = int(str(ceiling)) if ceiling is not None else None
    except ValueError as exc:
        raise ValueError("pod:maxViewRetrievals must be an integer") from exc

    return ViewSubmission(
        title=str(title),
        description=_value_str(graph, subject, DC_description),
        construct_template=str(template),
        content_type_hint=str(hint) if hint is not None else "text/turtle",
        params=params,
        max_view_retrievals=max_view_retrievals,
    )


def validate_construct_template(template: str) -> None:
    """Raise ValueError unless *template* is syntactically valid SPARQL CONSTRUCT."""
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
    max_view_retrievals: int | None = None,
) -> Graph:
    """Serialize a view definition into an rdflib Graph ready for ``write_system``."""
    graph = Graph()
    subject = URIRef(uri)
    graph.add((subject, RDF.type, POD_View))
    graph.add((subject, DC_title, RDFLiteral(title, datatype=XSD.string)))
    graph.add((subject, DC_description, RDFLiteral(description, datatype=XSD.string)))
    graph.add((subject, POD_constructTemplate, RDFLiteral(template, datatype=XSD.string)))
    graph.add((subject, POD_contentTypeHint, RDFLiteral(ct_hint, datatype=XSD.string)))
    # Mutable per-view delivery state seeded at creation so enforcement can bump it in place.
    graph.add((subject, POD_viewRetrievalCount, RDFLiteral(0, datatype=XSD.integer)))
    if max_view_retrievals is not None:
        graph.add(
            (subject, POD_maxViewRetrievals, RDFLiteral(max_view_retrievals, datatype=XSD.integer))
        )
    for param in params:
        pnode = BNode()
        graph.add((subject, POD_parameter, pnode))
        graph.add((pnode, POD_paramName, RDFLiteral(param.name, datatype=XSD.string)))
        graph.add((pnode, POD_paramType, RDFLiteral(param.type, datatype=XSD.string)))
    return graph


def parse_view_record(graph: Graph, uri: str) -> ViewRecord:
    """Reconstruct a ViewRecord from its stored RDF triples."""
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


_ABS_IRI = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*:.+")
# Characters an IRIREF may not contain (SPARQL grammar): control chars, space, and
# < > " { } | ^ ` \ . An iri value carrying any of these is not a serializable IRI term,
# so it is rejected at bind time (422) rather than failing later at term serialization.
_IRI_FORBIDDEN = re.compile('[\x00-\x20<>"{}|^`\\\\]')
_INT_ADAPTER: TypeAdapter[int] = TypeAdapter(int)


def bind_params(decls: list[ParamDecl], raw: dict[str, str]) -> dict[str, str]:
    """Validate supplied *raw* query-string values against *decls*; return an
    initBindings-ready dict.

    Parameters are optional: a declared parameter absent from *raw* is left
    unbound, so its SPARQL variable stays free and the view is not narrowed on
    that axis (a request with no parameters returns the view's full result).
    Only supplied values are type-checked and bound.
    """
    bound: dict[str, str] = {}
    for decl in decls:
        if decl.name not in raw:
            continue
        value = raw[decl.name]
        if decl.type == "int":
            try:
                _INT_ADAPTER.validate_python(value)
            except ValidationError as exc:
                raise ValueError(f"Parameter {decl.name!r} must be an integer") from exc
        elif decl.type == "iri" and (not _ABS_IRI.match(value) or _IRI_FORBIDDEN.search(value)):
            raise ValueError(f"Parameter {decl.name!r} must be an absolute IRI, got {value!r}")
        elif (
            decl.type in _PARAM_DATATYPE
            and RDFLiteral(value, datatype=_PARAM_DATATYPE[decl.type]).ill_typed
        ):
            # Validate with the same rdflib parser that binds the value at query time,
            # so a form accepted here can never turn ill-typed (and silently match
            # nothing) later. The datatype is applied by binding_datatypes/_to_term.
            raise ValueError(
                f"Parameter {decl.name!r} must be a valid {decl.type} value, got {value!r}"
            )
        bound[decl.name] = value
    return bound


def binding_datatypes(decls: list[ParamDecl]) -> dict[str, str]:
    """Map each declared date/dateTime parameter to its XSD datatype IRI."""
    return {
        decl.name: str(_PARAM_DATATYPE[decl.type]) for decl in decls if decl.type in _PARAM_DATATYPE
    }
