"""Container helpers: parent derivation, member-URI minting, and kind detection."""

import re
from uuid import uuid4

from rdflib import Graph
from rdflib.namespace import RDF
from rdflib.term import URIRef

from ldp_personal_store.vocab import (
    LDP_BasicContainer,
    LDP_Container,
    LDP_DirectContainer,
    LDP_RDFSource,
    LDP_Resource,
)

_SLUG_DISALLOWED = re.compile(r"[^a-z0-9-]+")
_SLUG_DASH_RUN = re.compile(r"-+")
_SLUG_MAX_LEN = 64


def parent_container_uri(uri: str, base_uri: str) -> str:
    """Return the container URI that holds the resource at *uri*.

    A direct child of the pod root maps back to *base_uri*; a deeper resource
    drops its last path segment and keeps the container trailing slash.
    """
    relative = uri.removeprefix(base_uri).rstrip("/")
    if "/" not in relative:
        return base_uri
    return base_uri + relative.rsplit("/", 1)[0] + "/"


def sanitize_slug(slug: str) -> str:
    """Reduce a client-supplied Slug header to a safe lowercase-dash segment."""
    collapsed = _SLUG_DASH_RUN.sub("-", _SLUG_DISALLOWED.sub("-", slug.lower()))
    return collapsed.strip("-")[:_SLUG_MAX_LEN].strip("-")


def mint_member_uri(container_uri: str, slug: str | None) -> str:
    """Mint a unique member URI under *container_uri* (guaranteed trailing slash).

    A usable *slug* contributes a sanitized human-readable prefix plus a short
    uuid suffix for uniqueness; otherwise a bare uuid4 hex segment is used.
    """
    if slug:
        sanitized = sanitize_slug(slug)
        if sanitized:
            return f"{container_uri}{sanitized}-{uuid4().hex[:8]}"
    return f"{container_uri}{uuid4().hex}"


def container_kind(graph: Graph, container_uri: str) -> str | None:
    """Return ``"basic"`` or ``"direct"`` for a container resource, else ``None``."""
    subject = URIRef(container_uri)
    if (subject, RDF.type, LDP_BasicContainer) in graph:
        return "basic"
    if (subject, RDF.type, LDP_DirectContainer) in graph:
        return "direct"
    return None


def container_link_types(kind: str) -> list[URIRef | str]:
    """Return the LDP ``rdf:type`` list for a container's ``Link`` header.

    The element type matches ``link_header``'s parameter so the result can be
    passed straight through without a list-invariance type error.
    """
    specific = LDP_BasicContainer if kind == "basic" else LDP_DirectContainer
    return [LDP_Resource, LDP_RDFSource, LDP_Container, specific]
