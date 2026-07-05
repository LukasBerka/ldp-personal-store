"""The engine->storage HTTP boundary: client, credential, and consumer validation.

The view engine reaches storage exclusively through :class:`StorageClient` — LDP
GETs for records and binaries, the SPARQL 1.1 Protocol endpoint for queries, and
the storage server's enforcement/log endpoints for its post-delivery writes. Every
request presents the engine's bearer token, so the pod owner can cut the engine
off at any time by revoking the ``.system/tokens/engine`` record. In the bundled
deployment the transport is an in-process ASGI bridge — the same HTTP surface with
no network socket; a split deployment points the client at a storage URL instead.

Consumer and owner tokens presented to the engine are validated here as well: the
engine holds no token records of its own, so it resolves a presented token by
querying storage (under its engine credential) and comparing hashes exactly as the
storage-side validator does.
"""

import hashlib
from typing import Annotated

import httpx
from fastapi import Depends, Request
from rdflib import Graph, URIRef

from app.auth.deps import require_bearer
from app.auth.tokens import (
    LOOKUP_QUERY,
    TokenRecord,
    match_token_rows,
    token_record_from_graph,
    unauthorized,
)
from app.vocab import POD_AdminToken, POD_ConsumerToken


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

    async def _query(self, sparql: str, bindings: dict[str, str] | None, accept: str):
        # Parameter values travel as binding-<name> protocol extension fields and are
        # bound server-side via initBindings — never spliced into the query text.
        data = {"query": sparql}
        if bindings:
            data.update({f"binding-{name}": value for name, value in bindings.items()})
        response = await self._http.post(
            self._storage_base + "sparql",
            data=data,
            headers={**self._headers, "Accept": accept},
        )
        self._expect(response, 200)
        return response

    async def construct(self, sparql: str, bindings: dict[str, str] | None = None) -> Graph:
        response = await self._query(sparql, bindings, "text/turtle")
        graph = Graph()
        graph.parse(data=response.text, format="turtle")
        return graph

    async def select(
        self, sparql: str, bindings: dict[str, str] | None = None
    ) -> list[dict[str, str]]:
        response = await self._query(sparql, bindings, "application/sparql-results+json")
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

    async def bump_token_enforcement(self, token_uri: str, count: int, last_used_at: str) -> None:
        response = await self._http.post(
            self._url(token_uri) + "/enforcement",
            json={"count": count, "last_used_at": last_used_at},
            headers=self._headers,
        )
        self._expect(response, 204)

    async def bump_view_enforcement(self, view_uri: str, count: int) -> None:
        response = await self._http.post(
            self._url(view_uri) + "/enforcement",
            json={"count": count},
            headers=self._headers,
        )
        self._expect(response, 204)

    async def append_access_log(self, view_uri: str, token_uri: str, timestamp: str) -> None:
        response = await self._http.post(
            self._storage_base + ".system/access-log",
            json={"view_uri": view_uri, "token_uri": token_uri, "timestamp": timestamp},
            headers=self._headers,
        )
        self._expect(response, 204)


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
    rows = await storage.select(LOOKUP_QUERY, bindings={"presented": presented_hash})
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
