"""Opaque bearer token minting, validation, revocation, and admin bootstrap.

Tokens are random URL-safe strings; only their SHA-256 hex digests are persisted
as RDF under ``.system/tokens/``. The plaintext is returned to the caller exactly
once at mint time and never enters the graph or touches disk. Validation hashes the
presented token, looks the record up by hash, and confirms the match in constant
time so a diverging comparison cannot leak through response latency.
"""

import hashlib
import hmac
import secrets
from dataclasses import dataclass

from fastapi import HTTPException
from rdflib import Graph, Literal, Namespace, URIRef, Variable
from rdflib.namespace import RDF, XSD

from app.storage.backend import ResourceNotFound, StorageBackend
from app.vocab import (
    POD_AdminToken,
    POD_ConsumerToken,
    POD_enforcementCount,
    POD_lastUsedAt,
    POD_linkedView,
    POD_policyRef,
    POD_Token,
    POD_tokenHash,
)

# Unix epoch: the initial lastUsedAt before any successful delivery bumps it.
_EPOCH = "1970-01-01T00:00:00Z"

_LOOKUP_QUERY = """
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
    linked_view_uri: str | None
    policy_ref: str | None
    enforcement_count: int
    last_used_at: str


def _unauthorized() -> HTTPException:
    # Missing, invalid, revoked, and wrong-type tokens all raise this identical 401
    # so a caller can never distinguish "no such token" from "valid but wrong type".
    return HTTPException(status_code=401, headers={"WWW-Authenticate": "Bearer"})


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
    linked_view_uri: str | None,
) -> str:
    token_uri = str(system_ns) + "tokens/" + record_id
    subject = URIRef(token_uri)
    graph = Graph()
    graph.add((subject, RDF.type, POD_Token))
    graph.add((subject, RDF.type, token_type))
    graph.add((subject, POD_tokenHash, Literal(token_hash, datatype=XSD.string)))
    if linked_view_uri is not None:
        graph.add((subject, POD_linkedView, URIRef(linked_view_uri)))
    # policyRef is a stable placeholder: the record shape carries the field now so the
    # policy resource it points at can be created and enforced later without a rewrite.
    graph.add((subject, POD_policyRef, URIRef(str(system_ns) + "tokens/policies/" + record_id)))
    graph.add((subject, POD_enforcementCount, Literal(0, datatype=XSD.integer)))
    graph.add((subject, POD_lastUsedAt, Literal(_EPOCH, datatype=XSD.dateTime)))
    backend.write_system(token_uri, graph)
    return token_uri


def mint_token(
    backend: StorageBackend,
    system_ns: Namespace,
    linked_view_uri: str | None = None,
    token_type: URIRef = POD_ConsumerToken,
) -> tuple[str, str]:
    """Generate a bearer token, persist a hash-only record, and return (plaintext, uri).

    The plaintext is the caller's single copy — it is never stored; only its SHA-256
    hex digest is written to the record under ``.system/tokens/``.
    """
    plaintext = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(plaintext.encode()).hexdigest()
    record_id = _allocate_record_id(backend, system_ns)
    token_uri = _write_record(
        backend, system_ns, record_id, token_hash, token_type, linked_view_uri
    )
    return plaintext, token_uri


def validate_token(
    backend: StorageBackend,
    raw_token: str,
    required_type: URIRef,
) -> TokenRecord:
    """Resolve *raw_token* to its record, or raise an indistinguishable 401.

    Hashes the presented token, looks the record up by hash, confirms the match with
    a constant-time compare, and requires *required_type* among the record's rdf:type
    markers. Every failure — not found, revoked, hash mismatch, wrong type — raises the
    same 401 body so validity and type never leak.
    """
    presented_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    rows = backend.query(_LOOKUP_QUERY, init_bindings={"presented": presented_hash}).bindings
    if not rows:
        raise _unauthorized()

    # One row per rdf:type triple: collapse them into the record's identity plus the
    # full set of its type markers.
    stored_hash: str | None = None
    token_uri: str | None = None
    types: set[str] = set()
    for row in rows:
        stored = row.get(Variable("stored"))
        if stored is not None:
            stored_hash = str(stored)
        uri = row.get(Variable("tokenUri"))
        if uri is not None:
            token_uri = str(uri)
        marker = row.get(Variable("tokenType"))
        if marker is not None:
            types.add(str(marker))

    if stored_hash is None or token_uri is None:
        raise _unauthorized()
    if not hmac.compare_digest(presented_hash, stored_hash):
        raise _unauthorized()
    if str(required_type) not in types:
        raise _unauthorized()

    subject = URIRef(token_uri)
    record = backend.read(token_uri)
    count = record.value(subject, POD_enforcementCount)
    view = record.value(subject, POD_linkedView)
    policy = record.value(subject, POD_policyRef)
    last_used = record.value(subject, POD_lastUsedAt)
    return TokenRecord(
        token_uri=token_uri,
        token_type=str(required_type),
        linked_view_uri=str(view) if view is not None else None,
        policy_ref=str(policy) if policy is not None else None,
        enforcement_count=int(str(count)) if count is not None else 0,
        last_used_at=str(last_used) if last_used is not None else _EPOCH,
    )


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
            None,
        )
        return None
    plaintext = secrets.token_urlsafe(32)
    _write_record(
        backend,
        system_ns,
        "admin",
        hashlib.sha256(plaintext.encode()).hexdigest(),
        POD_AdminToken,
        None,
    )
    return plaintext
