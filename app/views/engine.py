"""Consumer-facing view engine: the per-request RDF pipeline at ``/.engine/``.

``GET /.engine/views/{view_id}`` authenticates a consumer bearer token, confirms
the token is scoped to the requested view, loads the view definition from its
``.system/views/{id}`` record, binds query-string parameters, runs the view's
CONSTRUCT, serializes the result in the view's declared content type, and bumps
the enforcement counters — in that order. The CONSTRUCT re-runs on every request;
nothing is materialized or cached. It evaluates over the pod's public data only:
the reserved ``.system/`` records (views, tokens, policies, the access log) are
excluded from the storage query scope by default, so no view can leak them.

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

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import StreamingResponse
from rdflib import Graph, URIRef
from starlette.background import BackgroundTask

from app.apidocs import CONSUMER_AUTH, UNAUTHORIZED, rdf_response
from app.config import SettingsDep
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
    to deliveries, not attempts, because this runs only once the response is
    assured: after CONSTRUCT and serialization on the primary path, and after the
    upstream stream has opened on the blob path.
    """
    await storage.bump_token_enforcement(token_uri, token_count + 1, now)
    current_view = int(str(view_graph.value(URIRef(view_uri), POD_viewRetrievalCount) or 0))
    await storage.bump_view_enforcement(view_uri, current_view + 1)
    await storage.append_access_log(view_uri, token_uri, now)


@router.get(
    "/views/{view_id}",
    operation_id="getViewResult",
    summary="Fetch a view's result (consumer)",
    description=(
        "Run the view's CONSTRUCT and return the result in the view's declared content "
        "type. The bearer token must link this view (discover ids and parameter shapes "
        "at `/.engine/discovery`). Supply every declared parameter as a query-string "
        "field, `?name=value`. Pod-local resource references in the result — including "
        "binaries — are rewritten to `/.engine/blob/{view_id}?…` proxy URLs; dereference "
        "them as-is with the same token. Each successful delivery counts against the "
        "grant's policy ceilings; a denial is a `403` whose `detail` names the violated "
        "constraint (e.g. `policy: max retrievals reached`)."
    ),
    response_class=Response,
    responses={
        200: rdf_response("The view result, serialized as the view's content-type hint."),
        401: UNAUTHORIZED,
        403: {
            "description": (
                "The view is outside this grant's scope, or a policy constraint denied "
                "the request (`detail` names it)."
            )
        },
        404: {"description": "No view definition at this id."},
        422: {"description": "A declared parameter is missing or fails its type check."},
        502: {"description": "The engine could not reach storage (its credential may be revoked)."},
    },
    openapi_extra={"security": CONSUMER_AUTH},
)
async def get_view(
    view_id: str,
    request: Request,
    storage: StorageDep,
    token: EngineConsumerDep,
    settings: SettingsDep,
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
    out_graph = await rewrite_upstream_uris(
        result, settings.base_uri, engine_base, view_id, bound, storage
    )

    body = out_graph.serialize(format=rdflib_format_for(view.content_type_hint), encoding="utf-8")

    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    await _record_delivery(storage, token.token_uri, token.enforcement_count, view_uri, graph, now)

    return Response(content=body, media_type=view.content_type_hint)


@router.get(
    "/blob/{view_id}",
    operation_id="getViewBlob",
    summary="Dereference a proxied resource from a view result (consumer)",
    description=(
        "The gated proxy behind the rewritten URLs inside view results. Clients normally "
        "dereference those URLs verbatim — they already carry `uri` and the view's "
        "parameter bindings. The engine re-validates the token and scope, re-runs the "
        "view's CONSTRUCT with the supplied bindings, confirms the target still appears "
        "in the current result, and only then streams the resource with its stored media "
        "type. Deliveries count against the same policy ceilings as the primary view."
    ),
    response_class=Response,
    responses={
        200: {
            "description": "The resource bytes with their stored media type.",
            "content": {
                "application/octet-stream": {"schema": {"type": "string", "format": "binary"}}
            },
        },
        400: {
            "description": (
                "`uri` missing, it does not name a pod-local resource, or it names a "
                "reserved `.system/` record."
            )
        },
        401: UNAUTHORIZED,
        403: {
            "description": (
                "The view is outside this grant's scope, or a policy constraint denied "
                "the request (`detail` names it)."
            )
        },
        404: {
            "description": (
                "No view at this id, the target no longer appears in the view's current "
                "result, or the upstream resource is gone."
            )
        },
        422: {"description": "A declared parameter is missing or fails its type check."},
        502: {"description": "The engine could not reach storage (its credential may be revoked)."},
    },
    openapi_extra={
        "security": CONSUMER_AUTH,
        "parameters": [
            {
                "name": "uri",
                "in": "query",
                "required": True,
                "schema": {"type": "string"},
                "description": (
                    "The pod-local URI of the shared resource, percent-encoded — present "
                    "in rewritten proxy URLs already. The view's declared parameters must "
                    "accompany it with the same values used on the primary fetch."
                ),
            }
        ],
    },
)
async def get_blob(
    view_id: str,
    request: Request,
    storage: StorageDep,
    token: EngineConsumerDep,
    settings: SettingsDep,
) -> StreamingResponse:
    view_uri = str(request.app.state.system_ns) + "views/" + view_id
    if view_uri not in token.linked_view_uris:
        raise HTTPException(status_code=403)

    # Starlette already percent-decoded the query string once; decoding again here
    # would corrupt upstream URIs that legitimately contain %-sequences.
    upstream_uri = request.query_params.get("uri")
    if upstream_uri is None:
        raise HTTPException(status_code=400)
    # Open-proxy guard: only pod-local URIs may be dereferenced through this endpoint.
    if not upstream_uri.startswith(settings.base_uri):
        raise HTTPException(status_code=400)
    # A reserved .system/ record is never a legitimate view result. Refusing it
    # up front keeps the proxy from streaming server-managed records even if a
    # CONSTRUCT template emits one of their URIs as a constant.
    if upstream_uri.startswith(str(request.app.state.system_ns)):
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

    # Open the upstream stream before recording anything: counters and log must
    # stay faithful to deliveries, not attempts, and a missing upstream is a 404
    # that consumed no retrieval credit.
    try:
        upstream = await storage.open_binary_stream(upstream_uri)
    except UpstreamNotFound as exc:
        raise HTTPException(status_code=404) from exc

    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    await _record_delivery(storage, token.token_uri, token.enforcement_count, view_uri, graph, now)

    return StreamingResponse(
        upstream.aiter_bytes(),
        media_type=upstream.headers.get("content-type", "application/octet-stream"),
        background=BackgroundTask(upstream.aclose),
    )
