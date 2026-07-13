"""Consumer/owner token validation on the engine surface, over the storage HTTP contract."""

import hashlib
from typing import Annotated

from fastapi import Depends, Request
from rdflib import URIRef

from ldp_common.http import require_bearer
from ldp_common.tokenrecord import (
    LOOKUP_QUERY,
    TokenRecord,
    match_token_rows,
    token_record_from_graph,
    unauthorized,
)
from ldp_common.vocabulary import POD_AdminToken, POD_ConsumerToken
from ldp_view_engine.bindings import inject_values_block
from ldp_view_engine.client import StorageClient, UpstreamNotFound


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
    # The digest binds through a standard VALUES block (a SHA-256 hex string, so free of
    # any character that could escape the literal) and the lookup is scoped to the state
    # graph — no binding-* or include-system extension on the wire. The block is spliced by
    # the same WHERE-group scan the view engine uses, so it never depends on LOOKUP_QUERY's
    # exact byte layout the way a bare ``.replace("WHERE {", …)`` would.
    bound_lookup = inject_values_block(
        LOOKUP_QUERY, f'VALUES (?presented) {{ ("{presented_hash}") }}'
    )
    rows = await storage.select_state(storage.state_scoped(bound_lookup))
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
