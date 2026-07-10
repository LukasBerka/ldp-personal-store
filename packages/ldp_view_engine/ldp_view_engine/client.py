"""The engine->storage HTTP boundary: the client that speaks the pure standard contract.

Every call is standard LDP + SPARQL 1.1 over HTTP — conditional writes, container POST,
SPARQL query with a ``FROM``-named state graph and injected ``VALUES`` — so the engine
runs against any conforming store, our reference storage included.

The client addresses two logical endpoints that may be one server (the co-located default)
or two: the **state store** holds the engine's own records (tokens, views, policies, the
access log) and receives the enforcement writes; the **data source** answers the view
CONSTRUCTs and serves binaries. State reads/writes are always bearer-authenticated as the
engine; the data source carries its own configurable credential and namespace.
"""

import base64
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


def _with_trailing_slash(url: str) -> str:
    return url if url.endswith("/") else url + "/"


def _auth_headers(scheme: str, credential: str | None) -> dict[str, str]:
    """Authorization header for the data-source credential under *scheme*.

    ``bearer`` sends the credential as a bearer token; ``basic`` treats it as a
    ``user:password`` pair and HTTP-Basic-encodes it; ``none`` sends no credential
    (an open data source).
    """
    if scheme == "none" or credential is None:
        return {}
    if scheme == "basic":
        encoded = base64.b64encode(credential.encode()).decode()
        return {"Authorization": f"Basic {encoded}"}
    return {"Authorization": f"Bearer {credential}"}


class StorageClient:
    """Async client for the storage HTTP surface, authenticated as the view engine."""

    def __init__(
        self,
        http: httpx.AsyncClient,
        *,
        base_uri: str,
        state_token: str,
        state_url: str | None = None,
        data_url: str | None = None,
        data_base_uri: str | None = None,
        data_token: str | None = None,
        data_auth: str = "bearer",
        state_graph: str = "urn:ldp:engine-state",
    ) -> None:
        self._http = http
        # Engine-minted record URIs live under base_uri; a split state store rebases them
        # onto its own listening URL.
        self._base_uri = base_uri
        self._state_base = _with_trailing_slash(state_url if state_url is not None else base_uri)
        # The data source defaults to the state store when not separately configured.
        default_data = state_url if state_url is not None else base_uri
        self._data_base = _with_trailing_slash(data_url if data_url is not None else default_data)
        self._data_base_uri = data_base_uri if data_base_uri is not None else base_uri
        self._state_headers = {"Authorization": f"Bearer {state_token}"}
        self._data_headers = _auth_headers(
            data_auth, data_token if data_token is not None else state_token
        )
        self._state_graph = state_graph

    def _state_url(self, uri: str) -> str:
        return self._state_base + uri.removeprefix(self._base_uri)

    def _data_url(self, uri: str) -> str:
        return self._data_base + uri.removeprefix(self._data_base_uri)

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
        """LDP GET a state record (view/token/policy) from the state store."""
        response = await self._http.get(
            self._state_url(uri), headers={**self._state_headers, "Accept": "text/turtle"}
        )
        if response.status_code == 404:
            raise UpstreamNotFound(uri)
        self._expect(response, 200)
        graph = Graph()
        graph.parse(data=response.text, format="turtle")
        return graph

    async def _post_query(
        self, sparql: str, accept: str, base: str, headers: dict[str, str]
    ) -> httpx.Response:
        """POST a bare SPARQL 1.1 query — no protocol extensions on the engine's path.

        Parameters are already baked into the query text as an injected ``VALUES`` block,
        and scope is expressed with a standard ``FROM``, so the request needs neither the
        ``binding-*`` nor the ``include-system`` extension an arbitrary store would ignore.
        """
        response = await self._http.post(
            base + "sparql",
            content=sparql,
            headers={**headers, "Content-Type": "application/sparql-query", "Accept": accept},
        )
        self._expect(response, 200)
        return response

    async def construct(self, sparql: str) -> Graph:
        """Run a view CONSTRUCT against the data source."""
        response = await self._post_query(
            sparql, "text/turtle", self._data_base, self._data_headers
        )
        graph = Graph()
        graph.parse(data=response.text, format="turtle")
        return graph

    async def select_state(self, sparql: str) -> list[dict[str, str]]:
        """Run a SELECT against the state store (token lookup, access-log stats)."""
        return self._rows(
            await self._post_query(
                sparql, "application/sparql-results+json", self._state_base, self._state_headers
            )
        )

    async def select_data(self, sparql: str) -> list[dict[str, str]]:
        """Run a SELECT against the data source (e.g. listing proxiable resources)."""
        return self._rows(
            await self._post_query(
                sparql, "application/sparql-results+json", self._data_base, self._data_headers
            )
        )

    @staticmethod
    def _rows(response: httpx.Response) -> list[dict[str, str]]:
        rows = response.json()["results"]["bindings"]
        return [{name: cell["value"] for name, cell in row.items()} for row in rows]

    async def open_binary_stream(self, uri: str) -> httpx.Response:
        """Open a streaming LDP GET for a data-source binary; the caller must close it."""
        request = self._http.build_request("GET", self._data_url(uri), headers=self._data_headers)
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
            self._state_url(uri), headers={**self._state_headers, "Accept": "text/turtle"}
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
            self._state_url(uri),
            content=graph.serialize(format="turtle"),
            headers={**self._state_headers, "Content-Type": "text/turtle", "If-Match": etag},
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
            self._state_base + ".system/access-log",
            content=body,
            headers={**self._state_headers, "Content-Type": "text/turtle"},
        )
        self._expect(response, 201)
