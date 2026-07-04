"""Consumer-facing discovery container at ``/.engine/discovery``.

``GET /.engine/discovery`` authenticates a consumer bearer token and returns a
virtual LDP Basic Container listing only the view(s) that token unlocks. The
container is synthesized in memory on each request from the token's linked-view
reference; it is never persisted and does not pass through the LDP router.

A member is emitted only when the token is scoped to a view, so a token with no
linked view yields a valid but empty container rather than an error.
"""

from fastapi import APIRouter, Request, Response
from rdflib import Graph, URIRef
from rdflib.namespace import RDF

from app.auth.deps import AdminTokenDep, ConsumerTokenDep
from app.discovery.stats import StatsResponse, compute_stats
from app.ldp.content import link_header
from app.ldp.deps import BackendDep
from app.vocab import (
    LDP_BasicContainer,
    LDP_Container,
    LDP_contains,
    LDP_RDFSource,
    LDP_Resource,
)

router = APIRouter(prefix="/.engine", tags=["discovery"])


@router.get("/discovery")
def discover(request: Request, token: ConsumerTokenDep) -> Response:
    container_uri = str(request.app.state.engine_ns) + "discovery"
    subject = URIRef(container_uri)

    graph = Graph()
    graph.add((subject, RDF.type, LDP_Resource))
    graph.add((subject, RDF.type, LDP_RDFSource))
    graph.add((subject, RDF.type, LDP_BasicContainer))

    if token.linked_view_uri is not None:
        view_id = token.linked_view_uri.removeprefix(str(request.app.state.system_ns) + "views/")
        # The member is an engine-namespace URI so the consumer never learns the
        # reserved .system/ URI it is derived from.
        member = URIRef(str(request.app.state.engine_ns) + "views/" + view_id)
        graph.add((subject, LDP_contains, member))

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
def stats(request: Request, backend: BackendDep, token: AdminTokenDep) -> StatsResponse:
    # An owner management read over the access log, admin-gated and distinct from the
    # consumer-facing discovery listing above.
    return compute_stats(backend)
