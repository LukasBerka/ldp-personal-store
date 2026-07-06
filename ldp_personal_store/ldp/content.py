"""Content negotiation, serialization, ETag, and header helpers for the LDP layer.
"""

import hashlib
import re
from collections.abc import Iterable, Mapping

from fastapi import HTTPException
from rdflib import Graph
from rdflib.compare import to_canonical_graph
from rdflib.term import URIRef, Variable

from ldp_personal_store.storage.backend import StorageBackend
from ldp_personal_store.vocab import DC_format

# (media_type, rdflib_format) in server preference order.
SUPPORTED: list[tuple[str, str]] = [
    ("text/turtle", "turtle"),
    ("application/ld+json", "json-ld"),
    ("application/n-triples", "nt"),
    ("application/rdf+xml", "xml"),
]

RDF_CONTENT_TYPES: frozenset[str] = frozenset(media_type for media_type, _ in SUPPORTED)

FORMAT_BY_CONTENT_TYPE: dict[str, str] = dict(SUPPORTED)

# LDP servers that support POST MUST advertise the acceptable media types via an
# Accept-Post response header; the pod takes the four RDF syntaxes as RDF and any
# other type as a stored binary, hence the trailing */*.
ACCEPT_POST: str = ", ".join([media_type for media_type, _ in SUPPORTED] + ["*/*"])

LDP_PREFER_CONTAINMENT = "http://www.w3.org/ns/ldp#PreferContainment"
LDP_PREFER_MEMBERSHIP = "http://www.w3.org/ns/ldp#PreferMembership"
LDP_PREFER_MINIMAL_CONTAINER = "http://www.w3.org/ns/ldp#PreferMinimalContainer"

_PREFER_PARAM = re.compile(r'(include|omit)\s*=\s*"([^"]*)"', re.IGNORECASE)


def container_prefer(prefer: str | None) -> tuple[bool, bool, bool]:
    """Resolve an LDP container ``Prefer`` header into representation flags.

    Returns ``(include_containment, include_membership, applied)``. The default full
    representation carries both; ``ldp:PreferMinimalContainer`` drops both (unless the
    same header includes containment/membership back in), and an explicit ``omit`` of
    either drops just that one. ``applied`` is True when the header expressed a
    recognized representation preference, so the caller can echo ``Preference-Applied``.
    """
    if not prefer:
        return True, True, False
    params: dict[str, set[str]] = {"include": set(), "omit": set()}
    for keyword, value in _PREFER_PARAM.findall(prefer):
        params[keyword.lower()].update(value.split())
    include, omit = params["include"], params["omit"]
    if LDP_PREFER_MINIMAL_CONTAINER in include:
        containment = LDP_PREFER_CONTAINMENT in include
        membership = LDP_PREFER_MEMBERSHIP in include
    else:
        containment = LDP_PREFER_CONTAINMENT not in omit
        membership = LDP_PREFER_MEMBERSHIP not in omit
    recognized = {LDP_PREFER_CONTAINMENT, LDP_PREFER_MEMBERSHIP, LDP_PREFER_MINIMAL_CONTAINER}
    applied = bool((include | omit) & recognized)
    return containment, membership, applied


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


def parse_rdf_body(body: bytes, content_type: str | None, base_uri: str | None = None) -> Graph:
    """Parse a request body as RDF, or raise 415 (non-RDF type) / 400 (bad syntax).

    The shared guard for management routes that accept RDF representations, so the
    system surface admits exactly the same serializations as the LDP data plane.

    *base_uri* is the document base against which relative IRIs — most importantly
    the null relative IRI ``<>`` naming the resource itself — are resolved. Pass the
    target resource URI so ``<>`` becomes that resource; without it rdflib falls back
    to the server's working directory as a ``file://`` base, silently storing bogus
    ``file:///…/`` subjects.
    """
    normalized = normalize_media_type(content_type)
    if normalized not in RDF_CONTENT_TYPES:
        raise HTTPException(status_code=415)
    graph = Graph()
    try:
        graph.parse(data=body, format=FORMAT_BY_CONTENT_TYPE[normalized], publicID=base_uri)
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
    """Enforce If-Match / If-None-Match on a write, raising 412/428 on failure.

    An update (a PUT over an existing resource) must carry If-Match, so a blind
    overwrite cannot clobber a concurrent change; a bare update request is refused
    with 428 Precondition Required. If-Match then requires the ETag to match (or
    ``*`` for any existing resource); If-None-Match ``*`` forbids overwriting an
    existing one and is the create-only path, so it is exempt from the 428.
    """
    if resource_exists and if_match is None and if_none_match != "*":
        raise HTTPException(
            status_code=428,
            detail="If-Match required to update an existing resource",
        )
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
