"""Management router for the reserved ``.system/`` subtree.

Mounted ahead of the public LDP catch-all so ``.system/`` paths are adjudicated
here instead of reaching the public handlers. The whole surface speaks RDF, like
everything else the pod owner manages: a grant is minted by POSTing an RDF body
whose ``pod:linkedView`` objects name the views it unlocks (the one-time
plaintext comes back as ``pod:tokenSecret`` in the creation response and is never
persisted), a policy is authored by PUTting its RDF graph, and the ``.system/``
containers are browsable as LDP Basic Containers synthesized from the records'
type markers.

Writes require the pod owner's admin token. Reads accept either administrative
credential, but the engine's token is scoped to the record kinds the request
path needs (``views/`` and ``tokens/``, which includes ``tokens/policies/``);
creation, deletion, and wholesale rewriting of system resources remain in the
pod owner's hands.
"""

from typing import Annotated

from fastapi import APIRouter, Header, HTTPException, Request, Response
from rdflib import Graph, Literal, URIRef, Variable
from rdflib.namespace import RDF, XSD

from app.auth.deps import AdminTokenDep, StorageTokenDep
from app.auth.tokens import mint_token, revoke_token
from app.config import Settings, SettingsDep
from app.ldp.content import link_header, parse_rdf_body
from app.ldp.deps import BackendDep, RawBodyDep, http_error
from app.policy.enforce import parse_xsd_datetime
from app.storage.backend import ResourceNotFound, StorageBackend, StorageError
from app.vocab import (
    LDP_BasicContainer,
    LDP_Container,
    LDP_contains,
    LDP_RDFSource,
    LDP_Resource,
    POD_EngineToken,
    POD_expiresAt,
    POD_linkedView,
    POD_maxRetrievals,
    POD_minInterval,
    POD_Policy,
    POD_tokenSecret,
    POD_validFrom,
    POD_validUntil,
)

router = APIRouter(prefix="/.system", tags=["system"])

_ENGINE_READABLE_PREFIXES = ("views/", "tokens/")

# The synthesized ``.system/`` containers: each maps to the rdf:type marker whose
# instances it contains. Membership is derived from the union graph on every GET,
# so the listings are always current with no persisted container state.
_CONTAINER_MEMBER_QUERIES: dict[str, str] = {
    "views": "PREFIX pod: <urn:pod:vocab:> SELECT ?m WHERE { ?m a pod:View }",
    "tokens": "PREFIX pod: <urn:pod:vocab:> SELECT ?m WHERE { ?m a pod:Token }",
    "tokens/policies": "PREFIX pod: <urn:pod:vocab:> SELECT ?m WHERE { ?m a pod:Policy }",
    "access-log": "PREFIX pod: <urn:pod:vocab:> SELECT ?m WHERE { ?m a pod:AccessLogEntry }",
}

_CONTAINER_LINK = link_header([LDP_Resource, LDP_RDFSource, LDP_Container, LDP_BasicContainer])

_POLICY_CONSTRAINTS: tuple[tuple[URIRef, URIRef], ...] = (
    (POD_expiresAt, XSD.dateTime),
    (POD_validFrom, XSD.dateTime),
    (POD_validUntil, XSD.dateTime),
    (POD_maxRetrievals, XSD.integer),
    (POD_minInterval, XSD.integer),
)


def _validate_constraint(value: str, datatype: URIRef) -> None:
    """Raise ValueError unless *value* is a lexical form the enforcement layer can read."""
    if datatype == XSD.integer:
        int(value)
    else:
        parse_xsd_datetime(value)


@router.post("/tokens", status_code=201)
def issue_token(
    request: Request,
    backend: BackendDep,
    token: AdminTokenDep,
    body: RawBodyDep,
    content_type: Annotated[str | None, Header()] = None,
) -> Response:
    """Mint a grant from an RDF description of the views it unlocks.

    The response is the created record's representation plus one triple that
    exists nowhere else: ``pod:tokenSecret``, the plaintext bearer token,
    surfaced exactly once — only its SHA-256 hash is stored on the record.
    """
    graph = parse_rdf_body(body, content_type)
    linked = sorted(str(v) for v in graph.objects(None, POD_linkedView))
    plaintext, record_uri = mint_token(backend, request.app.state.system_ns, linked)
    out = backend.read(record_uri)
    out.add((URIRef(record_uri), POD_tokenSecret, Literal(plaintext, datatype=XSD.string)))
    return Response(
        status_code=201,
        content=out.serialize(format="turtle"),
        media_type="text/turtle",
        headers={"Location": record_uri},
    )


@router.put("/tokens/policies/{policy_id}")
def write_policy(
    policy_id: str,
    request: Request,
    backend: BackendDep,
    token: AdminTokenDep,
    body: RawBodyDep,
    content_type: Annotated[str | None, Header()] = None,
) -> Response:
    """Create or replace a policy from its RDF representation (full PUT, no merge).

    Exactly one subject typed ``pod:Policy`` is required — the shape a GET
    returns, so a GET-edit-PUT roundtrip with any LDP client works. Constraint
    values are re-rooted at the canonical policy URI and stored with their XSD
    datatypes; omitted constraints are cleared, unknown triples are dropped.
    """
    graph = parse_rdf_body(body, content_type)
    subjects = list(graph.subjects(RDF.type, POD_Policy))
    if len(subjects) != 1:
        raise HTTPException(
            status_code=422, detail="Body must describe exactly one pod:Policy resource"
        )
    policy_uri = str(request.app.state.system_ns) + "tokens/policies/" + policy_id
    canonical_subject = URIRef(policy_uri)
    canonical = Graph()
    canonical.add((canonical_subject, RDF.type, POD_Policy))
    for prop, datatype in _POLICY_CONSTRAINTS:
        value = graph.value(subjects[0], prop)
        if value is None:
            continue
        try:
            _validate_constraint(str(value), datatype)
        except ValueError as exc:
            raise HTTPException(
                status_code=422, detail=f"Invalid value for {prop}: {value}"
            ) from exc
        canonical.add((canonical_subject, prop, Literal(str(value), datatype=datatype)))

    try:
        backend.read(policy_uri)
        exists = True
    except ResourceNotFound:
        exists = False
    backend.write_system(policy_uri, canonical)
    return Response(
        status_code=200 if exists else 201,
        content=canonical.serialize(format="turtle"),
        media_type="text/turtle",
        headers={"Location": policy_uri},
    )


def _synthesize_container(key: str, backend: StorageBackend, settings: Settings) -> Response:
    container_uri = settings.base_uri + ".system/" + (key + "/" if key else "")
    subject = URIRef(container_uri)
    graph = Graph()
    graph.add((subject, RDF.type, LDP_Resource))
    graph.add((subject, RDF.type, LDP_RDFSource))
    graph.add((subject, RDF.type, LDP_BasicContainer))

    members: list[str] = []
    if key == "":
        members = [
            settings.base_uri + ".system/" + sub + "/" for sub in ("views", "tokens", "access-log")
        ]
    else:
        for row in backend.query(_CONTAINER_MEMBER_QUERIES[key]).bindings:
            member = row.get(Variable("m"))
            if member is not None:
                members.append(str(member))
        if key == "tokens":
            members.append(settings.base_uri + ".system/tokens/policies/")
    for member in sorted(members):
        graph.add((subject, LDP_contains, URIRef(member)))

    return Response(
        content=graph.serialize(format="turtle"),
        media_type="text/turtle",
        headers={"Link": _CONTAINER_LINK, "Allow": "GET, HEAD, OPTIONS"},
    )


@router.get("/{path:path}")
def read_system(
    path: str, backend: BackendDep, settings: SettingsDep, token: StorageTokenDep
) -> Response:
    if token.token_type == str(POD_EngineToken) and not path.startswith(
        _ENGINE_READABLE_PREFIXES
    ):
        raise HTTPException(status_code=403)
    key = path.rstrip("/")
    if key in _CONTAINER_MEMBER_QUERIES or path == "":
        return _synthesize_container(key, backend, settings)
    uri = settings.base_uri + ".system/" + path
    try:
        graph = backend.read(uri)
    except StorageError as exc:
        raise http_error(exc) from exc
    return Response(content=graph.serialize(format="turtle"), media_type="text/turtle")


@router.delete("/{path:path}", status_code=204)
def revoke_system(
    path: str, backend: BackendDep, settings: SettingsDep, token: AdminTokenDep
) -> Response:
    uri = settings.base_uri + ".system/" + path
    try:
        revoke_token(backend, uri)
    except StorageError as exc:
        raise http_error(exc) from exc
    return Response(status_code=204)
