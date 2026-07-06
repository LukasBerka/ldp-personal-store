"""Rewrite upstream resource URIs in a view's CONSTRUCT result into gated proxy URLs.

A view result may reference pod-local resources — RDF resources or binaries — by
their raw storage URI. Storage is gated, so a raw URI is a dead link for a
consumer; every URI that resolves to an upstream LDP resource is therefore
replaced, in both subject and object position, with an engine-namespace proxy URL
that dereferences through the gated blob endpoint under the same token, scope,
and policy checks as the primary representation. URIs that resolve to nothing,
blank nodes, and literals are copied through unchanged.

Detection is injection-safe: the set of existing resource URIs is fetched from
storage with a constant SPARQL query — no external input reaches the query text —
and intersected locally with the URIs appearing in the pod's own CONSTRUCT output.
Most stored resources are a named graph named by their URI; an LDP-NR is the
exception (its named graph is the description resource at ``{uri}.meta``), so the
query also surfaces binaries by their ``ldp:NonRDFSource`` type.
"""

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
    """Return a new graph with upstream resource URIs rewritten to engine proxy URLs.

    Every ``URIRef`` appearing as a subject or object that starts with *base_uri*
    and names an existing upstream resource is replaced by
    ``{engine_base}blob/{view_id}?uri={upstream}&{bound_params}``. The input
    *graph* is never mutated.
    """
    candidates: set[URIRef] = set()
    for subject, _, obj in graph:
        for term in (subject, obj):
            if isinstance(term, URIRef) and str(term).startswith(base_uri):
                candidates.add(term)
    if not candidates:
        return graph

    # Graph names live on the dataset's named-graph axis, which the default
    # system-excluding query scope does not expose, so this constant query opts
    # into the full dataset. The reserved .system/ names are dropped right here:
    # a server-managed record is never a rewrite target, so no proxy URL can be
    # minted for one even if a template smuggles its URI into the result.
    system_prefix = str(make_system_ns(base_uri))
    existing = {
        row["r"]
        for row in await storage.select(_RESOURCE_URIS_QUERY, include_system=True)
        if "r" in row and not row["r"].startswith(system_prefix)
    }
    mapping: dict[URIRef, URIRef] = {}
    for uri in candidates:
        if str(uri) in existing:
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
