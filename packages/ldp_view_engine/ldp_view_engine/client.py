"""The engine->storage HTTP boundary: the client that speaks the pure standard contract.

Every call is standard LDP + SPARQL 1.1 over HTTP — conditional writes, container POST,
SPARQL query with a ``FROM``-named state graph and injected ``VALUES`` — so the engine
runs against any conforming store, our reference storage included.
"""

import re

import httpx
from rdflib import Graph, Literal, URIRef
from rdflib.namespace import XSD

from ldp_common.vocab import (
    POD_enforcementCount,
    POD_lastUsedAt,
    POD_viewRetrievalCount,
)

# The engine bumps a shared counter with an optimistic conditional PUT; a concurrent
# delivery that wins the race invalidates the ETag, so a bounded retry re-reads and
# re-applies rather than losing the update. A single record is contended by at most the
# number of in-flight deliveries, so the cap sits comfortably above personal-scale
# concurrency (each contender commits once, so it can displace another at most once).
_ENFORCEMENT_MAX_RETRIES = 10


class UpstreamError(Exception):
    """Storage answered with an unexpected status (or refused the engine token)."""

    def __init__(self, status_code: int, detail: str = "") -> None:
        super().__init__(f"storage upstream returned {status_code}: {detail}")
        self.status_code = status_code


class UpstreamNotFound(UpstreamError):  # noqa: N818  intentional domain name
    def __init__(self, uri: str) -> None:
        super().__init__(404, uri)


class StorageClient:
    """Async client for the storage HTTP surface, authenticated as the view engine."""

    def __init__(
        self,
        http: httpx.AsyncClient,
        token: str,
        base_uri: str,
        storage_url: str | None = None,
        state_graph: str = "urn:ldp:engine-state",
    ) -> None:
        self._http = http
        self._headers = {"Authorization": f"Bearer {token}"}
        self._base_uri = base_uri
        self._storage_base = storage_url if storage_url is not None else base_uri
        if not self._storage_base.endswith("/"):
            self._storage_base += "/"
        self._state_graph = state_graph

    def _url(self, uri: str) -> str:
        # Resource URIs are minted under base_uri; a split deployment rebases them
        # onto the storage server's listening URL.
        return self._storage_base + uri.removeprefix(self._base_uri)

    def state_scoped(self, sparql: str) -> str:
        """Prefix a query's WHERE with a standard ``FROM`` naming the engine state graph.

        This is how the engine reaches its own state (token/view/policy records, the
        access log) without a proprietary scope flag: an arbitrary store resolves the
        ``FROM`` to that graph, and this reference server maps it onto its ``.system/``
        subtree. Queries without it evaluate over the data only, so view CONSTRUCTs can
        never see engine state.
        """
        return re.sub(
            r"\bWHERE\b", f"FROM <{self._state_graph}> WHERE", sparql, count=1, flags=re.IGNORECASE
        )

    @staticmethod
    def _expect(response: httpx.Response, status_code: int) -> None:
        if response.status_code != status_code:
            raise UpstreamError(response.status_code, response.text)

    async def read_graph(self, uri: str) -> Graph:
        response = await self._http.get(
            self._url(uri), headers={**self._headers, "Accept": "text/turtle"}
        )
        if response.status_code == 404:
            raise UpstreamNotFound(uri)
        self._expect(response, 200)
        graph = Graph()
        graph.parse(data=response.text, format="turtle")
        return graph

    async def _post_query(self, sparql: str, accept: str) -> httpx.Response:
        """POST a bare SPARQL 1.1 query — no protocol extensions on the engine's path.

        Parameters are already baked into the query text as an injected ``VALUES`` block,
        and scope is expressed with a standard ``FROM``, so the request needs neither the
        ``binding-*`` nor the ``include-system`` extension an arbitrary store would ignore.
        """
        response = await self._http.post(
            self._storage_base + "sparql",
            content=sparql,
            headers={**self._headers, "Content-Type": "application/sparql-query", "Accept": accept},
        )
        self._expect(response, 200)
        return response

    async def construct(self, sparql: str) -> Graph:
        response = await self._post_query(sparql, "text/turtle")
        graph = Graph()
        graph.parse(data=response.text, format="turtle")
        return graph

    async def select(self, sparql: str) -> list[dict[str, str]]:
        response = await self._post_query(sparql, "application/sparql-results+json")
        rows = response.json()["results"]["bindings"]
        return [{name: cell["value"] for name, cell in row.items()} for row in rows]

    async def open_binary_stream(self, uri: str) -> httpx.Response:
        """Open a streaming LDP GET for a binary; the caller must close the response."""
        request = self._http.build_request("GET", self._url(uri), headers=self._headers)
        response = await self._http.send(request, stream=True)
        if response.status_code != 200:
            await response.aclose()
            if response.status_code == 404:
                raise UpstreamNotFound(uri)
            raise UpstreamError(response.status_code)
        return response

    async def _read_record_with_etag(self, uri: str) -> tuple[Graph, str]:
        """LDP GET a state record, returning its graph and ETag for a conditional PUT."""
        response = await self._http.get(
            self._url(uri), headers={**self._headers, "Accept": "text/turtle"}
        )
        if response.status_code == 404:
            raise UpstreamNotFound(uri)
        self._expect(response, 200)
        etag = response.headers.get("ETag")
        if etag is None:
            raise UpstreamError(response.status_code, "storage record carried no ETag")
        graph = Graph()
        graph.parse(data=response.text, format="turtle")
        return graph, etag

    async def _conditional_put(self, uri: str, graph: Graph, etag: str) -> int:
        response = await self._http.put(
            self._url(uri),
            content=graph.serialize(format="turtle"),
            headers={**self._headers, "Content-Type": "text/turtle", "If-Match": etag},
        )
        return response.status_code

    async def bump_token_enforcement(self, token_uri: str, last_used_at: str) -> None:
        """Increment the grant's delivery counter via conditional read-modify-write PUT."""
        subject = URIRef(token_uri)
        for _ in range(_ENFORCEMENT_MAX_RETRIES):
            graph, etag = await self._read_record_with_etag(token_uri)
            count = int(str(graph.value(subject, POD_enforcementCount) or 0))
            graph.remove((subject, POD_enforcementCount, None))
            graph.remove((subject, POD_lastUsedAt, None))
            graph.add((subject, POD_enforcementCount, Literal(count + 1, datatype=XSD.integer)))
            graph.add((subject, POD_lastUsedAt, Literal(last_used_at, datatype=XSD.dateTime)))
            status = await self._conditional_put(token_uri, graph, etag)
            if status == 200:
                return
            if status != 412:
                raise UpstreamError(status)
        raise UpstreamError(412, "token enforcement update contended out")

    async def bump_view_enforcement(self, view_uri: str) -> None:
        """Increment the per-view retrieval counter via conditional read-modify-write PUT."""
        subject = URIRef(view_uri)
        for _ in range(_ENFORCEMENT_MAX_RETRIES):
            graph, etag = await self._read_record_with_etag(view_uri)
            count = int(str(graph.value(subject, POD_viewRetrievalCount) or 0))
            graph.remove((subject, POD_viewRetrievalCount, None))
            graph.add((subject, POD_viewRetrievalCount, Literal(count + 1, datatype=XSD.integer)))
            status = await self._conditional_put(view_uri, graph, etag)
            if status == 200:
                return
            if status != 412:
                raise UpstreamError(status)
        raise UpstreamError(412, "view enforcement update contended out")

    async def append_access_log(self, view_uri: str, token_uri: str, timestamp: str) -> None:
        """Append one delivery entry by POSTing it to the access-log LDP container."""
        body = (
            "@prefix pod: <urn:pod:vocab:> .\n"
            "@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .\n"
            "<> a pod:AccessLogEntry ;\n"
            f"   pod:accessLogView <{view_uri}> ;\n"
            f"   pod:accessLogToken <{token_uri}> ;\n"
            f'   pod:accessLogTimestamp "{timestamp}"^^xsd:dateTime .\n'
        )
        response = await self._http.post(
            self._storage_base + ".system/access-log",
            content=body,
            headers={**self._headers, "Content-Type": "text/turtle"},
        )
        self._expect(response, 201)
