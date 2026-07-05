"""Consumer-facing discovery container at ``/.engine/discovery``.

``GET /.engine/discovery`` authenticates a consumer bearer token (resolved through
the storage boundary under the engine's credential, like every engine request) and
returns a virtual LDP Basic Container listing the views that token unlocks,
together with each view's descriptive metadata: name, description, and parameter
shape. Metadata triples are re-rooted at the engine-namespace member URI and the
view's CONSTRUCT template is never included, so the reserved ``.system/`` names
and the owner's query internals stay invisible to consumers.

The container is synthesized in memory on each request from the token's
linked-view references; it is never persisted and does not pass through the LDP
router. A token with no linked views yields a valid but empty container rather
than an error.
"""

from fastapi import APIRouter, Request, Response
from rdflib import BNode, Graph, Literal, URIRef
from rdflib.namespace import RDF

from app.discovery.stats import StatsResponse, compute_stats
from app.ldp.content import link_header
from app.upstream import (
    EngineAdminDep,
    EngineConsumerDep,
    StorageClient,
    StorageDep,
    UpstreamNotFound,
)
from app.views.model import parse_view_record
from app.vocab import (
    DC_description,
    DC_title,
    LDP_BasicContainer,
    LDP_Container,
    LDP_contains,
    LDP_RDFSource,
    LDP_Resource,
    POD_parameter,
    POD_paramName,
    POD_paramType,
    POD_View,
)

router = APIRouter(prefix="/.engine", tags=["discovery"])


async def _describe_member(
    graph: Graph, member: URIRef, storage: StorageClient, view_uri: str
) -> None:
    """Add the view's consumer-facing metadata to *graph*, rooted at *member*.

    A linked view whose record no longer exists is listed without metadata — the
    stale grant is the owner's to clean up, and discovery stays total.
    """
    try:
        record = await storage.read_graph(view_uri)
    except UpstreamNotFound:
        return
    view = parse_view_record(record, view_uri)
    graph.add((member, RDF.type, POD_View))
    graph.add((member, DC_title, Literal(view.title)))
    graph.add((member, DC_description, Literal(view.description)))
    for param in view.params:
        pnode = BNode()
        graph.add((member, POD_parameter, pnode))
        graph.add((pnode, POD_paramName, Literal(param.name)))
        graph.add((pnode, POD_paramType, Literal(param.type)))


@router.get("/discovery")
async def discover(request: Request, storage: StorageDep, token: EngineConsumerDep) -> Response:
    engine_ns = str(request.app.state.engine_ns)
    system_views = str(request.app.state.system_ns) + "views/"
    container_uri = engine_ns + "discovery"
    subject = URIRef(container_uri)

    graph = Graph()
    graph.add((subject, RDF.type, LDP_Resource))
    graph.add((subject, RDF.type, LDP_RDFSource))
    graph.add((subject, RDF.type, LDP_BasicContainer))

    for view_uri in token.linked_view_uris:
        view_id = view_uri.removeprefix(system_views)
        # The member is an engine-namespace URI so the consumer never learns the
        # reserved .system/ URI it is derived from.
        member = URIRef(engine_ns + "views/" + view_id)
        graph.add((subject, LDP_contains, member))
        await _describe_member(graph, member, storage, view_uri)

    body = graph.serialize(format="turtle")
    return Response(
        content=body,
        media_type="text/turtle",
        headers={
            "Link": link_header([LDP_Resource, LDP_RDFSource, LDP_Container, LDP_BasicContainer]),
            "Allow": "GET, HEAD, OPTIONS",
        },
    )


@router.get("/stats")
async def stats(storage: StorageDep, token: EngineAdminDep) -> StatsResponse:
    # An owner management read over the access log, admin-gated and distinct from the
    # consumer-facing discovery listing above.
    return await compute_stats(storage)
