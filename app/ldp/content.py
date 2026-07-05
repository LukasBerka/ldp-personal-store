"""Content negotiation, serialization, ETag, and header helpers for the LDP layer.

Maps between HTTP media types and rdflib format tokens for the three RDF
syntaxes the pod serves, turns an ``Accept`` header into the response format to
use (or a 406 when nothing acceptable matches), derives stable ETags, builds
``Link``/``Allow`` header values, and evaluates conditional-request preconditions.
"""

import hashlib
from collections.abc import Iterable, Mapping

from fastapi import HTTPException
from rdflib import Graph
from rdflib.compare import to_canonical_graph
from rdflib.term import URIRef, Variable

from app.storage.backend import StorageBackend
from app.vocab import DC_format

# (media_type, rdflib_format) in server preference order.
SUPPORTED: list[tuple[str, str]] = [
    ("text/turtle", "turtle"),
    ("application/ld+json", "json-ld"),
    ("application/n-triples", "nt"),
    ("application/rdf+xml", "xml"),
]

RDF_CONTENT_TYPES: frozenset[str] = frozenset(media_type for media_type, _ in SUPPORTED)

FORMAT_BY_CONTENT_TYPE: dict[str, str] = dict(SUPPORTED)

ALLOW_RDF = "GET, HEAD, PUT, DELETE, OPTIONS"
ALLOW_CONTAINER = "GET, HEAD, POST, PUT, DELETE, OPTIONS"
ALLOW_BINARY = "GET, HEAD, PUT, DELETE, OPTIONS"


def normalize_media_type(value: str | None, default: str = "") -> str:
    """Strip parameters (``;charset=...``), whitespace, and case from a media type.

    A missing or empty header value yields *default*.
    """
    if not value:
        return default
    return value.split(";")[0].strip().lower() or default


def negotiate_media(
    accept: str | None, formats: Mapping[str, str], default_media: str
) -> tuple[str, str]:
    """Return ``(media_type, serializer_format)`` for the best entry of *accept*.

    Entries are matched in the client's listed order after stripping parameters;
    q-values are not weighed. A missing/empty header or a ``*/*`` entry selects
    *default_media*. Raises 406 when the header names no supported media type.
    """
    if not accept:
        return default_media, formats[default_media]
    for entry in accept.split(","):
        media_type = normalize_media_type(entry)
        if media_type == "*/*":
            return default_media, formats[default_media]
        if media_type in formats:
            return media_type, formats[media_type]
    raise HTTPException(status_code=406, detail=f"None of {sorted(formats)} acceptable")


def negotiate(accept: str | None) -> tuple[str, str]:
    """Return ``(rdflib_format, media_type)`` for the best RDF match of *accept*."""
    media_type, fmt = negotiate_media(accept, FORMAT_BY_CONTENT_TYPE, "text/turtle")
    return fmt, media_type


def rdflib_format_for(content_type: str) -> str:
    """Map an RDF request ``Content-Type`` to its rdflib parse format token.

    Parameters such as ``charset`` are stripped before lookup. The caller must
    confirm the type is in :data:`RDF_CONTENT_TYPES` first.
    """
    return FORMAT_BY_CONTENT_TYPE[normalize_media_type(content_type)]


def parse_rdf_body(body: bytes, content_type: str | None) -> Graph:
    """Parse a request body as RDF, or raise 415 (non-RDF type) / 400 (bad syntax).

    The shared guard for management routes that accept RDF representations, so the
    system surface admits exactly the same serializations as the LDP data plane.
    """
    normalized = normalize_media_type(content_type)
    if normalized not in RDF_CONTENT_TYPES:
        raise HTTPException(status_code=415)
    graph = Graph()
    try:
        graph.parse(data=body, format=FORMAT_BY_CONTENT_TYPE[normalized])
    except Exception as exc:  # rdflib parse errors span several exception types
        raise HTTPException(status_code=400, detail=f"Invalid RDF body: {exc}") from exc
    return graph


def etag_for_graph(graph: Graph) -> str:
    """Return a quoted, stable ETag for *graph*.

    Canonicalizing first gives blank nodes deterministic labels, and sorting the
    N-Triples lines removes traversal-order variance, so two graphs with the same
    triples yield the same ETag regardless of how each was assembled.
    """
    canonical = to_canonical_graph(graph)
    nt = canonical.serialize(format="nt")
    lines = sorted(nt.strip().splitlines())
    digest = hashlib.sha256("\n".join(lines).encode()).hexdigest()[:32]
    return f'"{digest}"'


def etag_for_binary(data: bytes) -> str:
    return '"' + hashlib.sha256(data).hexdigest()[:32] + '"'


def etag_for_stream(chunks: Iterable[bytes]) -> str:
    """Return a quoted ETag hashed incrementally over a chunked byte stream."""
    digest = hashlib.sha256()
    for chunk in chunks:
        digest.update(chunk)
    return '"' + digest.hexdigest()[:32] + '"'


def link_header(types: list[URIRef | str]) -> str:
    return ", ".join(f'<{t}>; rel="type"' for t in types)


def check_preconditions(
    if_match: str | None,
    if_none_match: str | None,
    current_etag: str | None,
    resource_exists: bool,
) -> None:
    """Enforce If-Match / If-None-Match on a write, raising 412 on failure.

    If-Match requires an existing resource whose ETag matches (or ``*`` for any
    existing resource); If-None-Match ``*`` forbids overwriting an existing one.
    """
    if if_match is not None:
        if if_match == "*" and not resource_exists:
            raise HTTPException(status_code=412)
        if if_match != "*" and (not resource_exists or current_etag != if_match):
            raise HTTPException(status_code=412)
    if if_none_match == "*" and resource_exists:
        raise HTTPException(status_code=412)


def binary_content_type(backend: StorageBackend, uri: str) -> str:
    """Return the stored media type for the binary resource at *uri*.

    Binary metadata lives in a sidecar graph that ``read`` does not expose, so the
    media type is fetched by querying its ``dcterms:format`` literal.
    """
    result = backend.query(
        f"SELECT ?ct WHERE {{ ?s <{DC_format}> ?ct }}",
        init_bindings={"s": uri},
    )
    for row in result.bindings:
        value = row.get(Variable("ct"))
        if value is not None:
            return str(value)
    return "application/octet-stream"
