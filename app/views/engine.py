"""Consumer-facing view engine: the per-request RDF pipeline at ``/.engine/``.

``GET /.engine/views/{view_id}`` authenticates a consumer bearer token, confirms
the token is scoped to the requested view, loads the view definition from its
``.system/views/{id}`` record, binds query-string parameters, runs the view's
CONSTRUCT, serializes the result in the view's declared content type, and bumps
the enforcement counters — in that order. The CONSTRUCT re-runs on every request;
nothing is materialized or cached.

Every storage interaction crosses the engine->storage HTTP boundary through the
:class:`~app.upstream.StorageClient` under the engine's own credential: record
loads are LDP GETs, queries go to the SPARQL Protocol endpoint (parameters as
``binding-`` extension fields), and the post-delivery counter and log writes hit
the storage server's enforcement endpoints. The engine touches no backend state
directly, so revoking the engine's token cuts it off from the pod entirely.

``GET /.engine/blob/{view_id}`` is the gated proxy for the upstream resource URIs
the primary handler rewrites — binaries and RDF resources alike. It re-validates
the same consumer token and scope, guards the decoded upstream URI against
open-proxy abuse, re-runs the view's CONSTRUCT to confirm the URI is still in the
current result (in subject or object position), and only then streams the
upstream bytes with the Content-Type the storage LDP surface reports — so a
consumer reaches a shared resource exclusively through this re-authorized path,
never storage directly.
"""

from datetime import UTC, datetime
from urllib.parse import unquote

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import StreamingResponse
from rdflib import Graph, URIRef
from starlette.background import BackgroundTask

from app.config import get_settings
from app.ldp.content import rdflib_format_for
from app.policy.enforce import check_policy
from app.upstream import EngineConsumerDep, StorageClient, StorageDep, UpstreamNotFound
from app.views.model import ViewRecord, bind_params, parse_view_record
from app.views.rewrite import rewrite_upstream_uris
from app.vocab import POD_viewRetrievalCount

router = APIRouter(prefix="/.engine", tags=["engine"])


async def _load_view(storage: StorageClient, view_uri: str) -> tuple[Graph, ViewRecord]:
    try:
        graph = await storage.read_graph(view_uri)
    except UpstreamNotFound as exc:
        raise HTTPException(status_code=404) from exc
    return graph, parse_view_record(graph, view_uri)


async def _load_policy(storage: StorageClient, policy_ref: str | None) -> Graph | None:
    if policy_ref is None:
        return None
    try:
        return await storage.read_graph(policy_ref)
    except UpstreamNotFound:
        # The policy URI is a stable placeholder on every token; when no graph was
        # ever written there the grant is unconstrained.
        return None


async def _record_delivery(
    storage: StorageClient,
    token_uri: str,
    token_count: int,
    view_uri: str,
    view_graph: Graph,
    now: str,
) -> None:
    """Bump both counters and append the access-log entry for a confirmed delivery.

    The +1 rides the count read at validate time; the storage server applies each
    write atomically per record, and the read-to-bump race is the documented
    TOCTOU window accepted for a single-user pod. Counters and log stay faithful
    to deliveries, not attempts, because this runs only after a successful
    CONSTRUCT (and serialization, on the primary path).
    """
    await storage.bump_token_enforcement(token_uri, token_count + 1, now)
    current_view = int(str(view_graph.value(URIRef(view_uri), POD_viewRetrievalCount) or 0))
    await storage.bump_view_enforcement(view_uri, current_view + 1)
    await storage.append_access_log(view_uri, token_uri, now)


@router.get("/views/{view_id}")
async def get_view(
    view_id: str,
    request: Request,
    storage: StorageDep,
    token: EngineConsumerDep,
) -> Response:
    view_uri = str(request.app.state.system_ns) + "views/" + view_id
    if view_uri not in token.linked_view_uris:
        raise HTTPException(status_code=403)

    graph, view = await _load_view(storage, view_uri)

    try:
        bound = bind_params(view.params, dict(request.query_params))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    # Policy decision on the fully-validated request, before any data is produced.
    check_policy(token, await _load_policy(storage, token.policy_ref), graph, view_uri)

    result = await storage.construct(view.construct_template, bindings=bound)

    # Replace raw storage URIs of shared resources with gated engine proxy URLs so
    # the consumer follows every reference through the engine, never storage directly.
    engine_base = str(request.app.state.engine_ns)
    base_uri = get_settings().base_uri
    out_graph = await rewrite_upstream_uris(result, base_uri, engine_base, view_id, bound, storage)

    body = out_graph.serialize(format=rdflib_format_for(view.content_type_hint), encoding="utf-8")

    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    await _record_delivery(storage, token.token_uri, token.enforcement_count, view_uri, graph, now)

    return Response(content=body, media_type=view.content_type_hint)


@router.get("/blob/{view_id}")
async def get_blob(
    view_id: str,
    request: Request,
    storage: StorageDep,
    token: EngineConsumerDep,
) -> StreamingResponse:
    view_uri = str(request.app.state.system_ns) + "views/" + view_id
    if view_uri not in token.linked_view_uris:
        raise HTTPException(status_code=403)

    raw = request.query_params.get("uri")
    if raw is None:
        raise HTTPException(status_code=400)
    upstream_uri = unquote(raw)
    base_uri = get_settings().base_uri
    # Open-proxy guard: only pod-local URIs may be dereferenced through this endpoint.
    if not upstream_uri.startswith(base_uri):
        raise HTTPException(status_code=400)

    graph, view = await _load_view(storage, view_uri)

    # The forwarded "uri" key is not a declared param, so bind_params ignores it. A view
    # that happened to declare a param literally named "uri" is an accepted edge case:
    # its binding would be overridden by the proxy's upstream URI.
    try:
        bound = bind_params(view.params, dict(request.query_params))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    # Policy decision on the fully-validated request, before any data is produced.
    check_policy(token, await _load_policy(storage, token.policy_ref), graph, view_uri)

    result = await storage.construct(view.construct_template, bindings=bound)

    # A stale proxy URL — the resource is no longer produced by the view — is
    # unreachable. Membership means appearing anywhere the rewrite step looks:
    # subject or object position of the current result.
    result_terms = {
        str(term)
        for subject, _, obj in result
        for term in (subject, obj)
        if isinstance(term, URIRef)
    }
    if upstream_uri not in result_terms:
        raise HTTPException(status_code=404)

    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    await _record_delivery(storage, token.token_uri, token.enforcement_count, view_uri, graph, now)

    try:
        upstream = await storage.open_binary_stream(upstream_uri)
    except UpstreamNotFound as exc:
        raise HTTPException(status_code=404) from exc
    return StreamingResponse(
        upstream.aiter_bytes(),
        media_type=upstream.headers.get("content-type", "application/octet-stream"),
        background=BackgroundTask(upstream.aclose),
    )
