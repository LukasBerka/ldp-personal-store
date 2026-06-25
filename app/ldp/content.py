"""Content negotiation and RDF serialization helpers for the LDP layer.

Maps between HTTP media types and rdflib format tokens for the three RDF
syntaxes the pod serves, and turns an ``Accept`` header into the response format
to use (or a 406 when nothing acceptable matches).
"""

from fastapi import HTTPException
from rdflib import Graph

# (media_type, rdflib_format) in server preference order.
SUPPORTED: list[tuple[str, str]] = [
    ("text/turtle", "turtle"),
    ("application/ld+json", "json-ld"),
    ("application/n-triples", "nt"),
]

RDF_CONTENT_TYPES: frozenset[str] = frozenset(
    {"text/turtle", "application/ld+json", "application/n-triples"}
)

_FORMAT_BY_CONTENT_TYPE: dict[str, str] = dict(SUPPORTED)


def negotiate(accept: str | None) -> tuple[str, str]:
    """Return ``(rdflib_format, media_type)`` for the best match of *accept*.

    Defaults to Turtle for a missing/empty header or ``*/*``; raises 406 when the
    header is present but names no supported RDF media type.
    """
    if not accept or accept == "*/*":
        return "turtle", "text/turtle"
    for media_type, fmt in SUPPORTED:
        if media_type in accept:
            return fmt, media_type
    raise HTTPException(
        status_code=406,
        detail=f"None of {[media_type for media_type, _ in SUPPORTED]} acceptable",
    )


def rdflib_format_for(content_type: str) -> str:
    """Map an RDF request ``Content-Type`` to its rdflib parse format token.

    Parameters such as ``charset`` are stripped before lookup. The caller must
    confirm the type is in :data:`RDF_CONTENT_TYPES` first.
    """
    normalized = content_type.split(";")[0].strip().lower()
    return _FORMAT_BY_CONTENT_TYPE[normalized]


def serialize_graph(graph: Graph, fmt: str) -> str:
    return graph.serialize(format=fmt)
