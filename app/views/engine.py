"""Consumer-facing view engine: the per-request RDF pipeline at ``/.engine/``.

``GET /.engine/views/{view_id}`` authenticates a consumer bearer token, confirms
the token is scoped to the requested view, loads the view definition from its
``.system/views/{id}`` record, binds query-string parameters as injection-safe
initBindings, runs the view's CONSTRUCT, serializes the result in the view's
declared content type, and bumps the token's enforcement counter — in that order.
The CONSTRUCT re-runs on every request; nothing is materialized or cached.

The view resource always loads from the ``.system/views/{id}`` URI the token's
linked-view reference points at. The ``.engine/`` namespace is only the route
prefix, never where the view definition lives.
"""

from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Request, Response

from app.auth.deps import ConsumerTokenDep
from app.config import get_settings
from app.ldp.content import rdflib_format_for
from app.ldp.deps import BackendDep
from app.policy.enforce import check_policy
from app.storage.backend import ResourceNotFound
from app.views.binary import rewrite_binary_uris
from app.views.model import bind_params, parse_view_record

router = APIRouter(prefix="/.engine", tags=["engine"])


@router.get("/views/{view_id}")
def get_view(
    view_id: str,
    request: Request,
    backend: BackendDep,
    token: ConsumerTokenDep,
) -> Response:
    view_uri = str(request.app.state.system_ns) + "views/" + view_id
    if token.linked_view_uri != view_uri:
        raise HTTPException(status_code=403)

    try:
        graph = backend.read(view_uri)
    except ResourceNotFound as exc:
        raise HTTPException(status_code=404) from exc
    view = parse_view_record(graph, view_uri)

    try:
        bound = bind_params(view.params, dict(request.query_params))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    # Policy decision on the fully-validated request, before any data is produced.
    check_policy(token, backend)

    result = backend.query(view.construct_template, init_bindings=bound)
    if result.graph is None:
        raise HTTPException(status_code=500, detail="view query returned no graph")

    # Replace raw storage URIs of shared binaries with gated engine proxy URLs so the
    # consumer never dereferences pod storage directly.
    engine_base = str(request.app.state.engine_ns)
    base_uri = get_settings().base_uri
    out_graph = rewrite_binary_uris(result.graph, base_uri, engine_base, view_id, bound, backend)

    fmt = rdflib_format_for(view.content_type_hint)
    body = out_graph.serialize(format=fmt, encoding="utf-8")

    # The +1 rides the count read at validate time; update_enforcement writes it
    # atomically under an RLock, which is acceptable for a single-user pod. The
    # counter bumps only here, after a successful CONSTRUCT and serialization.
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    backend.update_enforcement(token.token_uri, token.enforcement_count + 1, now)

    return Response(content=body, media_type=view.content_type_hint)
