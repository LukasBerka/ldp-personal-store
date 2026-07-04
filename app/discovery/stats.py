"""On-demand statistics over the append-only access log.

The owner reads aggregate and per-view delivery counts by aggregating the
``pod:AccessLogEntry`` resources the delivery paths append under
``.system/access-log/``. The engine runs both aggregations over the storage
boundary's SPARQL endpoint, where every log entry is its own named-graph context
in the backend's union graph, so a graph-clause-free WHERE sees them all.
"""

from pydantic import BaseModel

from app.upstream import StorageClient

# The POD vocabulary is a fixed urn: namespace, never derived from the pod's base URI.
_TOTAL_SPARQL = (
    "PREFIX pod: <urn:pod:vocab:> "
    "SELECT (COUNT(?e) AS ?total) WHERE { ?e a pod:AccessLogEntry }"
)
_BY_VIEW_SPARQL = (
    "PREFIX pod: <urn:pod:vocab:> "
    "SELECT ?view (COUNT(?e) AS ?count) "
    "WHERE { ?e a pod:AccessLogEntry ; pod:accessLogView ?view } "
    "GROUP BY ?view ORDER BY DESC(?count)"
)


class ViewCount(BaseModel):
    view_uri: str
    count: int


class StatsResponse(BaseModel):
    total: int
    by_view: list[ViewCount]


async def compute_stats(storage: StorageClient) -> StatsResponse:
    """Aggregate the access log into a total and a per-view delivery breakdown."""
    total = 0
    for row in await storage.select(_TOTAL_SPARQL):
        if "total" in row:
            total = int(row["total"])

    by_view: list[ViewCount] = []
    for row in await storage.select(_BY_VIEW_SPARQL):
        if "view" not in row or "count" not in row:
            continue
        by_view.append(ViewCount(view_uri=row["view"], count=int(row["count"])))

    return StatsResponse(total=total, by_view=by_view)
