"""Consumer-facing view engine: the per-request RDF pipeline at ``/.engine/``."""

from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import StreamingResponse
from rdflib import Graph, URIRef
from starlette.background import BackgroundTask

from ldp_personal_store.apidocs import CONSUMER_AUTH, UNAUTHORIZED, rdf_response
from ldp_personal_store.config import SettingsDep
from ldp_personal_store.ldp.content import rdflib_format_for
from ldp_personal_store.policy.enforce import check_policy
from ldp_personal_store.upstream import (
    EngineConsumerDep,
    StorageClient,
    StorageDep,
    UpstreamNotFound,
)
from ldp_personal_store.views.bindings import BindingError, inject_values
from ldp_personal_store.views.model import (
    ViewRecord,
    bind_params,
    parse_view_record,
)
from ldp_personal_store.views.rewrite import rewrite_upstream_uris

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
        return None


async def _record_delivery(
    storage: StorageClient,
    token_uri: str,
    view_uri: str,
    now: str,
) -> None:
    """Bump both counters and append the access-log entry for a confirmed delivery.

    Each write is a standard LDP call to storage: the counters through conditional
    read-modify-write PUTs (which read the current value themselves), the log entry
    through a container POST.
    """
    await storage.bump_token_enforcement(token_uri, now)
    await storage.bump_view_enforcement(view_uri)
    await storage.append_access_log(view_uri, token_uri, now)


@router.get(
    "/views/{view_id}",
    operation_id="getViewResult",
    summary="Fetch a view's result (consumer)",
    description=(
        "Run the view's CONSTRUCT and return the result in the view's declared content "
        "type. The bearer token must link this view (discover ids and parameter shapes "
        "at `/.engine/discovery`). Declared parameters are optional: supply any as a "
        "query-string field, `?name=value`, to narrow the result; omit them to receive "
        "the view's full, un-narrowed output. Pod-local resource references in the "
        "result — including "
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
        422: {"description": "A supplied parameter fails its declared type check."},
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

    # Parameters bind through an injected VALUES block in the query text — the portable,
    # injection-safe equivalent of initBindings — so the data source needs no extension.
    try:
        query = inject_values(view.construct_template, bound, view.params)
    except BindingError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    result = await storage.construct(query)

    # Replace raw storage URIs of shared resources with gated engine proxy URLs so
    # the consumer follows every reference through the engine, never storage directly.
    engine_base = str(request.app.state.engine_ns)
    out_graph = await rewrite_upstream_uris(
        result, settings.base_uri, engine_base, view_id, bound, storage
    )

    body = out_graph.serialize(format=rdflib_format_for(view.content_type_hint), encoding="utf-8")

    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    await _record_delivery(storage, token.token_uri, view_uri, now)

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
        422: {"description": "A supplied parameter fails its declared type check."},
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
                    "in rewritten proxy URLs already. Any view parameters used on the "
                    "primary fetch must accompany it with the same values, so the proxy "
                    "re-runs the identical (equally narrowed) CONSTRUCT."
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

    upstream_uri = request.query_params.get("uri")
    if upstream_uri is None:
        raise HTTPException(status_code=400)
    if not upstream_uri.startswith(settings.base_uri):
        raise HTTPException(status_code=400)
    if upstream_uri.startswith(str(request.app.state.system_ns)):
        raise HTTPException(status_code=400)

    graph, view = await _load_view(storage, view_uri)

    try:
        bound = bind_params(view.params, dict(request.query_params))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    check_policy(token, await _load_policy(storage, token.policy_ref), graph, view_uri)

    try:
        query = inject_values(view.construct_template, bound, view.params)
    except BindingError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    result = await storage.construct(query)

    result_terms = {
        str(term)
        for subject, _, obj in result
        for term in (subject, obj)
        if isinstance(term, URIRef)
    }
    if upstream_uri not in result_terms:
        raise HTTPException(status_code=404)

    try:
        upstream = await storage.open_binary_stream(upstream_uri)
    except UpstreamNotFound as exc:
        raise HTTPException(status_code=404) from exc

    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    await _record_delivery(storage, token.token_uri, view_uri, now)

    return StreamingResponse(
        upstream.aiter_bytes(),
        media_type=upstream.headers.get("content-type", "application/octet-stream"),
        background=BackgroundTask(upstream.aclose),
    )
