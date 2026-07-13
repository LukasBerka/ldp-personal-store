"""Storage-side opaque bearer token issuance, validation, revocation, and bootstrap.

The shared record model and hash-lookup helpers live in :mod:`ldp_common.tokenrecord`;
this module is the reference store's local validator and the owner's issuance path.
"""

import hashlib
import secrets
from collections.abc import Sequence

from rdflib import Graph, Literal, Namespace, URIRef
from rdflib.namespace import RDF, XSD

from ldp_common.tokenrecord import (
    EPOCH,
    LOOKUP_QUERY,
    TokenRecord,
    match_token_rows,
    token_record_from_graph,
    unauthorized,
)
from ldp_common.vocabulary import (
    DC_title,
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
from ldp_personal_store.storage.backend import ResourceNotFound, StorageBackend


def _allocate_record_id(backend: StorageBackend, system_ns: Namespace) -> str:
    record_id = secrets.token_urlsafe(8)
    try:
        backend.read(str(system_ns) + "tokens/" + record_id)
    except ResourceNotFound:
        return record_id
    raise RuntimeError("could not allocate a unique token record id")


def _write_record(
    backend: StorageBackend,
    system_ns: Namespace,
    record_id: str,
    token_hash: str,
    token_type: URIRef,
    linked_view_uris: Sequence[str],
    name: str | None = None,
) -> str:
    token_uri = str(system_ns) + "tokens/" + record_id
    subject = URIRef(token_uri)
    graph = Graph()
    graph.add((subject, RDF.type, POD_Token))
    graph.add((subject, RDF.type, token_type))
    graph.add((subject, POD_tokenHash, Literal(token_hash, datatype=XSD.string)))
    # Optional owner-chosen label so a grant reads as "colleagues" or "access-bob"
    # in listings instead of only its opaque record id.
    if name:
        graph.add((subject, DC_title, Literal(name, datatype=XSD.string)))
    for view_uri in linked_view_uris:
        graph.add((subject, POD_linkedView, URIRef(view_uri)))
    # policyRef is a stable placeholder: the record shape carries the field now so the
    # policy resource it points at can be created and enforced later without a rewrite.
    graph.add((subject, POD_policyRef, URIRef(str(system_ns) + "tokens/policies/" + record_id)))
    graph.add((subject, POD_enforcementCount, Literal(0, datatype=XSD.integer)))
    graph.add((subject, POD_lastUsedAt, Literal(EPOCH, datatype=XSD.dateTime)))
    backend.write_system(token_uri, graph)
    return token_uri


def issue_token(
    backend: StorageBackend,
    system_ns: Namespace,
    linked_view_uris: Sequence[str] = (),
    token_type: URIRef = POD_ConsumerToken,
    name: str | None = None,
) -> tuple[str, str]:
    """Generate a bearer token, persist a hash-only record, and return (plaintext, uri).

    A single grant may unlock any number of views — one ``pod:linkedView`` triple
    per entry of *linked_view_uris*. An optional *name* is stored as a
    ``dcterms:title`` label so the pod owner can recognise the grant by an intent
    such as "colleagues" rather than by its opaque record id. The plaintext is the
    caller's single copy — it is never stored; only its SHA-256 hex digest is
    written to the record under ``.system/tokens/``.
    """
    plaintext = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(plaintext.encode()).hexdigest()
    record_id = _allocate_record_id(backend, system_ns)
    token_uri = _write_record(
        backend, system_ns, record_id, token_hash, token_type, linked_view_uris, name
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
    result = backend.query(
        LOOKUP_QUERY, init_bindings={"presented": presented_hash}, include_system=True
    )
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
    admin_token: str,
) -> None:
    """Reconcile the admin token record to the operator-supplied value on every boot.

    The admin credential is required and supplied out of band (``LDP_ADMIN_TOKEN``);
    this writes the record at the fixed ``admin`` id holding only the SHA-256 hash of
    that value. The plaintext is never persisted or logged. Rewriting on every boot
    makes rotation a matter of restarting with a new ``LDP_ADMIN_TOKEN``.
    """
    _write_record(
        backend,
        system_ns,
        "admin",
        hashlib.sha256(admin_token.encode()).hexdigest(),
        POD_AdminToken,
        (),
    )


def bootstrap_engine_token(
    backend: StorageBackend,
    system_ns: Namespace,
    engine_token: str | None = None,
) -> str:
    """Seed the engine's storage credential at a fixed record id; return the plaintext.

    The record lives at ``.system/tokens/engine`` so the pod owner can revoke the
    engine's read access at any time by deleting it. With *engine_token* supplied
    (split deployments, tests) its hash is seeded deterministically; otherwise a
    fresh random token is issued on every startup and the plaintext exists only in
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
