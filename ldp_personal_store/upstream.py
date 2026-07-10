"""The engine->storage HTTP boundary: client, credential, and consumer validation."""

import hashlib
from typing import Annotated

import httpx
from fastapi import Depends, Request
from rdflib import Graph, Literal, URIRef
from rdflib.namespace import XSD

from ldp_personal_store.auth.deps import require_bearer
from ldp_personal_store.auth.tokens import (
    LOOKUP_QUERY,
    TokenRecord,
    match_token_rows,
    token_record_from_graph,
    unauthorized,
)
from ldp_personal_store.vocab import (
    POD_AdminToken,
    POD_ConsumerToken,
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
    ) -> None:
        self._http = http
        self._headers = {"Authorization": f"Bearer {token}"}
        self._base_uri = base_uri
        self._storage_base = storage_url if storage_url is not None else base_uri
        if not self._storage_base.endswith("/"):
            self._storage_base += "/"

    def _url(self, uri: str) -> str:
        # Resource URIs are minted under base_uri; a split deployment rebases them
        # onto the storage server's listening URL.
        return self._storage_base + uri.removeprefix(self._base_uri)

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

    async def _query(
        self,
        sparql: str,
        bindings: dict[str, str] | None,
        accept: str,
        include_system: bool,
        binding_types: dict[str, str] | None = None,
    ):
        # Parameter values travel as binding-<name> protocol extension fields and are
        # bound server-side via initBindings — never spliced into the query text. A
        # companion bindingtype-<name> field carries the XSD datatype for typed
        # (date/dateTime) parameters so they bind as typed terms, not plain literals.
        data = {"query": sparql}
        if bindings:
            data.update({f"binding-{name}": value for name, value in bindings.items()})
        if binding_types:
            data.update({f"bindingtype-{name}": dt for name, dt in binding_types.items()})
        # Queries evaluate over the pod's public data by default; the .system/
        # records stay out of scope unless the caller opts in explicitly.
        if include_system:
            data["include-system"] = "true"
        response = await self._http.post(
            self._storage_base + "sparql",
            data=data,
            headers={**self._headers, "Accept": accept},
        )
        self._expect(response, 200)
        return response

    async def construct(
        self,
        sparql: str,
        bindings: dict[str, str] | None = None,
        include_system: bool = False,
        binding_types: dict[str, str] | None = None,
    ) -> Graph:
        response = await self._query(sparql, bindings, "text/turtle", include_system, binding_types)
        graph = Graph()
        graph.parse(data=response.text, format="turtle")
        return graph

    async def select(
        self,
        sparql: str,
        bindings: dict[str, str] | None = None,
        include_system: bool = False,
        binding_types: dict[str, str] | None = None,
    ) -> list[dict[str, str]]:
        response = await self._query(
            sparql, bindings, "application/sparql-results+json", include_system, binding_types
        )
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


async def validate_via_storage(
    storage: StorageClient,
    raw_token: str,
    required_type: URIRef,
) -> TokenRecord:
    """Resolve a token presented to the engine through the storage HTTP surface.

    Hashes the presented token, finds candidate records by digest over the SPARQL
    endpoint, requires *required_type* among the matching record's markers, and
    reads the record over the system surface — every failure raises the same 401.
    The matching and record-assembly steps are the storage-side validator's own
    helpers, so the two validators cannot drift.
    """
    presented_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    rows = await storage.select(
        LOOKUP_QUERY, bindings={"presented": presented_hash}, include_system=True
    )
    token_uri, matched_type = match_token_rows(rows, presented_hash, (required_type,))
    try:
        record = await storage.read_graph(token_uri)
    except UpstreamNotFound as exc:
        raise unauthorized() from exc
    return token_record_from_graph(record, token_uri, matched_type)


def get_storage(request: Request) -> StorageClient:
    return request.app.state.storage


StorageDep = Annotated[StorageClient, Depends(get_storage)]


async def get_engine_consumer_token(
    raw: Annotated[str, Depends(require_bearer)],
    storage: StorageDep,
) -> TokenRecord:
    return await validate_via_storage(storage, raw, POD_ConsumerToken)


async def get_engine_admin_token(
    raw: Annotated[str, Depends(require_bearer)],
    storage: StorageDep,
) -> TokenRecord:
    return await validate_via_storage(storage, raw, POD_AdminToken)


EngineConsumerDep = Annotated[TokenRecord, Depends(get_engine_consumer_token)]
EngineAdminDep = Annotated[TokenRecord, Depends(get_engine_admin_token)]
