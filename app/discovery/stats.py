"""On-demand statistics over the append-only access log.

The owner reads aggregate and per-view delivery counts by aggregating the
``pod:AccessLogEntry`` resources the delivery paths append under
``.system/access-log/``. Both queries run over the backend's union graph, where
every log entry is its own named-graph context, so a graph-clause-free WHERE sees
them all without any per-graph plumbing.
"""

from pydantic import BaseModel
from rdflib.term import Variable

from app.storage.backend import StorageBackend

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


def compute_stats(backend: StorageBackend) -> StatsResponse:
    """Aggregate the access log into a total and a per-view delivery breakdown."""
    total = 0
    for row in backend.query(_TOTAL_SPARQL).bindings:
        value = row.get(Variable("total"))
        if value is not None:
            total = int(str(value))

    by_view: list[ViewCount] = []
    for row in backend.query(_BY_VIEW_SPARQL).bindings:
        view = row.get(Variable("view"))
        count = row.get(Variable("count"))
        if view is None or count is None:
            continue
        by_view.append(ViewCount(view_uri=str(view), count=int(str(count))))

    return StatsResponse(total=total, by_view=by_view)
