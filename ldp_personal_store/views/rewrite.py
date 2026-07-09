"""Rewrite upstream resource URIs in a view's CONSTRUCT result into gated proxy URLs."""

from urllib.parse import quote

from rdflib import Graph, URIRef
from rdflib.term import Node

from ldp_personal_store.upstream import StorageClient
from ldp_personal_store.vocab import make_system_ns

_RESOURCE_URIS_QUERY = (
    "SELECT DISTINCT ?r WHERE {"
    "  { GRAPH ?r { ?s ?p ?o } }"
    "  UNION"
    "  { ?r a <http://www.w3.org/ns/ldp#NonRDFSource> }"
    "}"
)


async def rewrite_upstream_uris(
    graph: Graph,
    base_uri: str,
    engine_base: str,
    view_id: str,
    bound_params: dict[str, str],
    storage: StorageClient,
) -> Graph:
    """Return a new graph with upstream resource URIs rewritten to engine proxy URLs."""
    candidates: set[URIRef] = set()
    for subject, _, obj in graph:
        for term in (subject, obj):
            if isinstance(term, URIRef) and str(term).startswith(base_uri):
                candidates.add(term)
    if not candidates:
        return graph

    system_prefix = str(make_system_ns(base_uri))
    existing = {
        row["r"]
        for row in await storage.select(_RESOURCE_URIS_QUERY, include_system=True)
        if "r" in row and not row["r"].startswith(system_prefix)
    }
    mapping: dict[URIRef, URIRef] = {}
    for uri in candidates:
        if str(uri) in existing:
            pairs = {"uri": str(uri), **bound_params}
            query_string = "&".join(
                f"{quote(key, safe='')}={quote(value, safe='')}" for key, value in pairs.items()
            )
            mapping[uri] = URIRef(f"{engine_base}blob/{view_id}?{query_string}")

    def remap(term: Node) -> Node:
        return mapping[term] if isinstance(term, URIRef) and term in mapping else term

    out = Graph()
    for subject, predicate, obj in graph:
        out.add((remap(subject), predicate, remap(obj)))
    return out
