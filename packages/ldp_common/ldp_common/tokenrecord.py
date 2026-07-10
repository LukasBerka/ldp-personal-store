"""Shared bearer-token record model and hash-lookup helpers.

Both validators — the storage server's local one and the engine's over-HTTP one —
resolve a presented token through these helpers, so the two can never drift.
"""

import hmac
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

from fastapi import HTTPException
from rdflib import Graph, URIRef

from ldp_common.vocab import (
    POD_enforcementCount,
    POD_lastUsedAt,
    POD_linkedView,
    POD_policyRef,
)

# Unix epoch: the initial lastUsedAt before any successful delivery bumps it.
EPOCH = "1970-01-01T00:00:00Z"

LOOKUP_QUERY = """
PREFIX pod: <urn:pod:vocab:>
SELECT ?tokenUri ?stored ?tokenType WHERE {
    ?tokenUri pod:tokenHash ?stored ;
              a ?tokenType .
    FILTER(str(?stored) = str(?presented))
}
"""


@dataclass(frozen=True)
class TokenRecord:
    token_uri: str
    token_type: str
    # Every view this token unlocks (one pod:linkedView triple each). Empty for
    # unscoped tokens such as the admin and engine credentials.
    linked_view_uris: tuple[str, ...]
    policy_ref: str | None
    enforcement_count: int
    last_used_at: str


def unauthorized() -> HTTPException:
    return HTTPException(status_code=401, headers={"WWW-Authenticate": "Bearer"})


def match_token_rows(
    rows: Iterable[Mapping[str, str]],
    presented_hash: str,
    allowed_types: Sequence[URIRef],
) -> tuple[str, URIRef]:
    """Resolve hash-lookup rows to ``(token_uri, matched_type)`` or raise the 401.

    One row arrives per rdf:type triple.
    """
    types_by_uri: dict[str, set[str]] = {}
    hash_by_uri: dict[str, str] = {}
    for row in rows:
        uri = row.get("tokenUri")
        if uri is None:
            continue
        stored = row.get("stored")
        if stored is not None:
            hash_by_uri[uri] = stored
        marker = row.get("tokenType")
        if marker is not None:
            types_by_uri.setdefault(uri, set()).add(marker)

    for uri in sorted(types_by_uri):
        stored_hash = hash_by_uri.get(uri)
        if stored_hash is None or not hmac.compare_digest(presented_hash, stored_hash):
            continue
        matched = next((t for t in allowed_types if str(t) in types_by_uri[uri]), None)
        if matched is not None:
            return uri, matched
    raise unauthorized()


def token_record_from_graph(graph: Graph, token_uri: str, token_type: URIRef) -> TokenRecord:
    """Assemble a :class:`TokenRecord` from the record's stored triples."""
    subject = URIRef(token_uri)
    count = graph.value(subject, POD_enforcementCount)
    views = tuple(sorted(str(v) for v in graph.objects(subject, POD_linkedView)))
    policy = graph.value(subject, POD_policyRef)
    last_used = graph.value(subject, POD_lastUsedAt)
    return TokenRecord(
        token_uri=token_uri,
        token_type=str(token_type),
        linked_view_uris=views,
        policy_ref=str(policy) if policy is not None else None,
        enforcement_count=int(str(count)) if count is not None else 0,
        last_used_at=str(last_used) if last_used is not None else EPOCH,
    )
