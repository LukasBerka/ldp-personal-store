"""Shared view-definition model: typed records, RDF read-back, and injection-safe
parameter binding. The storage-side authoring half (submission parsing, template
validation, RDF serialization) lives in :mod:`ldp_personal_store.views.submission`.
"""

import re
from typing import Literal

from pydantic import BaseModel, TypeAdapter, ValidationError
from rdflib import Graph, URIRef
from rdflib import Literal as RDFLiteral
from rdflib.namespace import XSD
from rdflib.term import Node

from ldp_common.vocabulary import (
    DC_description,
    DC_title,
    POD_constructTemplate,
    POD_contentTypeHint,
    POD_parameter,
    POD_paramName,
    POD_paramType,
)

ParamTypeName = Literal["str", "int", "iri", "date", "dateTime"]

# Declared parameter types that bind as a typed RDF literal rather than a plain one,
# mapped to the XSD datatype the value carries into the query.
_PARAM_DATATYPE: dict[str, URIRef] = {"date": XSD.date, "dateTime": XSD.dateTime}

# Same scheme test the storage backend uses to decide URIRef-vs-Literal coercion, so a
# value bound portably (injected VALUES) is the identical term the initBindings path made.
_TERM_IRI_SCHEME = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*:")


def param_term(value: str, param_type: ParamTypeName) -> URIRef | RDFLiteral:
    """The rdflib term a validated parameter *value* of *param_type* binds as."""
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


def _value_str(graph: Graph, subject: Node, predicate: URIRef) -> str:
    value = graph.value(subject, predicate)
    return str(value) if value is not None else ""


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
