"""Rewrite upstream binary URIs in a view's CONSTRUCT result into gated proxy URLs.

A view result may reference pod-local binary resources (``ldp:NonRDFSource``) by
their raw storage URI. Handing those URIs to a consumer would let it point at
storage directly, bypassing the token/policy boundary. :func:`rewrite_binary_uris`
replaces every such URI — in both subject and object position — with an
engine-namespace proxy URL that routes back through the gated blob endpoint.

Detection is injection-safe: the set of binary URIs is fetched from storage with a
constant SPARQL query (no external input reaches the query text) and intersected
locally with the URIs appearing in the pod's own CONSTRUCT output.
"""

from urllib.parse import quote

from rdflib import Graph, URIRef
from rdflib.term import Node

from app.upstream import StorageClient

_BINARY_SUBJECTS_QUERY = (
    "PREFIX ldp: <http://www.w3.org/ns/ldp#> SELECT ?s WHERE { ?s a ldp:NonRDFSource }"
)


async def rewrite_binary_uris(
    graph: Graph,
    base_uri: str,
    engine_base: str,
    view_id: str,
    bound_params: dict[str, str],
    storage: StorageClient,
) -> Graph:
    """Return a new graph with pod-local binary URIs rewritten to engine proxy URLs.

    Every ``URIRef`` appearing as a subject or object that starts with *base_uri*
    and is typed ``ldp:NonRDFSource`` is replaced by
    ``{engine_base}blob/{view_id}?uri={upstream}&{bound_params}``. Non-binary URIs,
    blank nodes, and literals are copied through unchanged, and the input *graph* is
    never mutated.
    """
    candidates: set[URIRef] = set()
    for subject, _, obj in graph:
        for term in (subject, obj):
            if isinstance(term, URIRef) and str(term).startswith(base_uri):
                candidates.add(term)
    if not candidates:
        return graph

    binaries = {row["s"] for row in await storage.select(_BINARY_SUBJECTS_QUERY)}
    mapping: dict[URIRef, URIRef] = {}
    for uri in candidates:
        if str(uri) in binaries:
            # The original param bindings are re-encoded into the proxy URL so the
            # blob endpoint can re-run the identical CONSTRUCT to re-authorize access.
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
