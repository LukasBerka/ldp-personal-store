"""Opaque bearer token minting, validation, revocation, and admin bootstrap.

Tokens are random URL-safe strings; only their SHA-256 hex digests are persisted
as RDF under ``.system/tokens/``. The plaintext is returned to the caller exactly
once at mint time and never enters the graph or touches disk. Validation hashes
the presented token and resolves the record by digest equality; only digests of
high-entropy random tokens are ever compared, so equality checks reveal nothing
useful about any plaintext.

The pure pieces of validation — the hash-lookup query, grouping the lookup rows
into candidate records, and assembling a :class:`TokenRecord` from a record graph
— are shared with the engine-side validator in :mod:`app.upstream`, which runs
the same steps over the storage HTTP boundary instead of the backend.
"""

import hashlib
import hmac
import secrets
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

from fastapi import HTTPException
from rdflib import Graph, Literal, Namespace, URIRef
from rdflib.namespace import RDF, XSD

from app.storage.backend import ResourceNotFound, StorageBackend
from app.vocab import (
    POD_AdminToken,
    POD_ConsumerToken,
    POD_enforcementCount,
    POD_EngineToken,
    POD_lastUsedAt,
    POD_linkedView,
    POD_policyRef,
    POD_Token,
    POD_tokenHash,
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

_ADMIN_EXISTS_QUERY = """
PREFIX pod: <urn:pod:vocab:>
ASK { ?t a pod:AdminToken }
"""


@dataclass(frozen=True)
class TokenRecord:
    token_uri: str
    token_type: str
    # Every view this grant unlocks (one pod:linkedView triple each); empty for
    # unscoped tokens such as the admin and engine credentials.
    linked_view_uris: tuple[str, ...]
    policy_ref: str | None
    enforcement_count: int
    last_used_at: str


def unauthorized() -> HTTPException:
    # Missing, invalid, revoked, and wrong-type tokens all raise this identical 401
    # so a caller can never distinguish "no such token" from "valid but wrong type".
    return HTTPException(status_code=401, headers={"WWW-Authenticate": "Bearer"})


def match_token_rows(
    rows: Iterable[Mapping[str, str]],
    presented_hash: str,
    allowed_types: Sequence[URIRef],
) -> tuple[str, URIRef]:
    """Resolve hash-lookup rows to ``(token_uri, matched_type)`` or raise the 401.

    One row arrives per rdf:type triple. Rows are grouped by ``tokenUri`` before
    anything is compared, so two records that happen to carry the same hash (the
    same plaintext seeded twice) can never mix one record's identity with
    another's type markers. The first candidate record — in sorted-URI order, for
    determinism — whose hash matches and whose markers include one of
    *allowed_types* wins; no candidate means the indistinguishable 401.
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


def _allocate_record_id(backend: StorageBackend, system_ns: Namespace) -> str:
    # A fresh short id is collision-free in practice for a personal pod; the single
    # retry covers the astronomically unlikely case of hitting an existing record.
    for _ in range(2):
        record_id = secrets.token_urlsafe(8)
        try:
            backend.read(str(system_ns) + "tokens/" + record_id)
        except ResourceNotFound:
            return record_id
    raise RuntimeError("could not allocate a unique token record id after one retry")


def _write_record(
    backend: StorageBackend,
    system_ns: Namespace,
    record_id: str,
    token_hash: str,
    token_type: URIRef,
    linked_view_uris: Sequence[str],
) -> str:
    token_uri = str(system_ns) + "tokens/" + record_id
    subject = URIRef(token_uri)
    graph = Graph()
    graph.add((subject, RDF.type, POD_Token))
    graph.add((subject, RDF.type, token_type))
    graph.add((subject, POD_tokenHash, Literal(token_hash, datatype=XSD.string)))
    for view_uri in linked_view_uris:
        graph.add((subject, POD_linkedView, URIRef(view_uri)))
    # policyRef is a stable placeholder: the record shape carries the field now so the
    # policy resource it points at can be created and enforced later without a rewrite.
    graph.add((subject, POD_policyRef, URIRef(str(system_ns) + "tokens/policies/" + record_id)))
    graph.add((subject, POD_enforcementCount, Literal(0, datatype=XSD.integer)))
    graph.add((subject, POD_lastUsedAt, Literal(EPOCH, datatype=XSD.dateTime)))
    backend.write_system(token_uri, graph)
    return token_uri


def mint_token(
    backend: StorageBackend,
    system_ns: Namespace,
    linked_view_uris: Sequence[str] = (),
    token_type: URIRef = POD_ConsumerToken,
) -> tuple[str, str]:
    """Generate a bearer token, persist a hash-only record, and return (plaintext, uri).

    A single grant may unlock any number of views — one ``pod:linkedView`` triple
    per entry of *linked_view_uris*. The plaintext is the caller's single copy — it
    is never stored; only its SHA-256 hex digest is written to the record under
    ``.system/tokens/``.
    """
    plaintext = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(plaintext.encode()).hexdigest()
    record_id = _allocate_record_id(backend, system_ns)
    token_uri = _write_record(
        backend, system_ns, record_id, token_hash, token_type, linked_view_uris
    )
    return plaintext, token_uri


def validate_token_one_of(
    backend: StorageBackend,
    raw_token: str,
    allowed_types: Sequence[URIRef],
) -> TokenRecord:
    """Resolve *raw_token* to its record, or raise an indistinguishable 401.

    Hashes the presented token, looks candidate records up by digest, and requires
    at least one of *allowed_types* among the matching record's rdf:type markers.
    Every failure — not found, revoked, hash mismatch, wrong type — raises the same
    401 body so validity and type never leak.
    """
    presented_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    result = backend.query(LOOKUP_QUERY, init_bindings={"presented": presented_hash})
    rows = [{str(var): str(term) for var, term in row.items()} for row in result.bindings]
    token_uri, matched_type = match_token_rows(rows, presented_hash, allowed_types)
    try:
        record = backend.read(token_uri)
    except ResourceNotFound as exc:
        raise unauthorized() from exc
    return token_record_from_graph(record, token_uri, matched_type)


def validate_token(
    backend: StorageBackend,
    raw_token: str,
    required_type: URIRef,
) -> TokenRecord:
    """Single-type convenience wrapper over :func:`validate_token_one_of`."""
    return validate_token_one_of(backend, raw_token, (required_type,))


def revoke_token(backend: StorageBackend, record_uri: str) -> None:
    """Delete the token record so every later validation finds nothing and returns 401."""
    backend.delete_system(record_uri)


def bootstrap_admin_token(
    backend: StorageBackend,
    system_ns: Namespace,
    admin_token: str | None = None,
) -> str | None:
    """Seed the admin token record when none exists; return the plaintext only if generated.

    Idempotent: with an admin record already present it does nothing. When *admin_token*
    is supplied its hash is seeded and None is returned (the caller already knows the
    value); otherwise a random token is generated and returned so the caller can log it
    once. The plaintext is never persisted — only its hash.
    """
    if backend.query(_ADMIN_EXISTS_QUERY).askAnswer:
        return None
    if admin_token is not None:
        _write_record(
            backend,
            system_ns,
            "admin",
            hashlib.sha256(admin_token.encode()).hexdigest(),
            POD_AdminToken,
            (),
        )
        return None
    plaintext = secrets.token_urlsafe(32)
    _write_record(
        backend,
        system_ns,
        "admin",
        hashlib.sha256(plaintext.encode()).hexdigest(),
        POD_AdminToken,
        (),
    )
    return plaintext


def bootstrap_engine_token(
    backend: StorageBackend,
    system_ns: Namespace,
    engine_token: str | None = None,
) -> str:
    """Seed the engine's storage credential at a fixed record id; return the plaintext.

    The record lives at ``.system/tokens/engine`` so the pod owner can revoke the
    engine's read access at any time by deleting it. With *engine_token* supplied
    (split deployments, tests) its hash is seeded deterministically; otherwise a
    fresh random token is minted on every startup and the plaintext exists only in
    process memory — the bundled engine is handed it directly and nothing else ever
    needs it. Only the SHA-256 hash is persisted either way.
    """
    plaintext = engine_token if engine_token is not None else secrets.token_urlsafe(32)
    _write_record(
        backend,
        system_ns,
        "engine",
        hashlib.sha256(plaintext.encode()).hexdigest(),
        POD_EngineToken,
        (),
    )
    return plaintext
