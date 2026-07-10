"""On-demand statistics over the append-only access log."""

from pydantic import BaseModel

from ldp_view_engine.client import StorageClient

# The POD vocabulary is a fixed urn: namespace, never derived from the pod's base URI.
_TOTAL_SPARQL = (
    "PREFIX pod: <urn:pod:vocab:> SELECT (COUNT(?e) AS ?total) WHERE { ?e a pod:AccessLogEntry }"
)
_BY_VIEW_SPARQL = (
    "PREFIX pod: <urn:pod:vocab:> "
    "SELECT ?view (COUNT(?e) AS ?count) (MAX(?ts) AS ?last) "
    "WHERE { ?e a pod:AccessLogEntry ; pod:accessLogView ?view ; "
    "pod:accessLogTimestamp ?ts } "
    "GROUP BY ?view ORDER BY DESC(?count)"
)
_BY_TOKEN_SPARQL = (
    "PREFIX pod: <urn:pod:vocab:> "
    "SELECT ?token (COUNT(?e) AS ?count) "
    "WHERE { ?e a pod:AccessLogEntry ; pod:accessLogToken ?token } "
    "GROUP BY ?token ORDER BY DESC(?count)"
)


class ViewCount(BaseModel):
    view_uri: str
    count: int
    last_accessed_at: str


class TokenCount(BaseModel):
    token_uri: str
    count: int


class StatsResponse(BaseModel):
    total: int
    by_view: list[ViewCount]
    by_token: list[TokenCount]


async def compute_stats(storage: StorageClient) -> StatsResponse:
    """Aggregate the access log into a total plus per-view and per-consumer breakdowns."""
    total = 0
    for row in await storage.select_state(storage.state_scoped(_TOTAL_SPARQL)):
        if "total" in row:
            total = int(row["total"])

    by_view: list[ViewCount] = []
    for row in await storage.select_state(storage.state_scoped(_BY_VIEW_SPARQL)):
        if "view" not in row or "count" not in row:
            continue
        by_view.append(
            ViewCount(
                view_uri=row["view"],
                count=int(row["count"]),
                last_accessed_at=row.get("last", ""),
            )
        )

    by_token: list[TokenCount] = []
    for row in await storage.select_state(storage.state_scoped(_BY_TOKEN_SPARQL)):
        if "token" not in row or "count" not in row:
            continue
        by_token.append(TokenCount(token_uri=row["token"], count=int(row["count"])))

    return StatsResponse(total=total, by_view=by_view, by_token=by_token)
