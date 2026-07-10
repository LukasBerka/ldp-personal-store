"""Management router for the reserved ``.system/`` subtree."""

from typing import Annotated

from fastapi import APIRouter, Header, HTTPException, Request, Response
from rdflib import Graph, Literal, URIRef, Variable
from rdflib.namespace import RDF, XSD

from ldp_personal_store.apidocs import (
    ADMIN_AUTH,
    STORAGE_AUTH,
    UNAUTHORIZED,
    rdf_request_body,
    turtle_response,
)
from ldp_personal_store.auth.deps import AdminTokenDep, StorageTokenDep
from ldp_personal_store.auth.tokens import issue_token, revoke_token
from ldp_personal_store.config import Settings, SettingsDep
from ldp_personal_store.ldp.content import etag_for_graph, link_header, parse_rdf_body
from ldp_personal_store.ldp.deps import BackendDep, RawBodyDep, http_error
from ldp_personal_store.policy.enforce import parse_xsd_datetime
from ldp_personal_store.storage.backend import ResourceNotFound, StorageBackend, StorageError
from ldp_personal_store.vocab import (
    DC_title,
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


@router.post(
    "/tokens",
    status_code=201,
    operation_id="issueToken",
    summary="Issue a consumer grant",
    response_class=Response,
    responses={
        201: turtle_response(
            "The created record. `pod:tokenSecret` is the consumer's plaintext bearer "
            "token, surfaced only here and never retrievable again; `dcterms:title` "
            "echoes the grant's optional owner-chosen name; `pod:policyRef` names the "
            "policy resource to `PUT` for bounding this grant; `Location` names the "
            "record for later revocation.",
            "@prefix pod: <urn:pod:vocab:> .\n"
            "@prefix dcterms: <http://purl.org/dc/terms/> .\n\n"
            "<https://pod.example/.system/tokens/NGlxYzZa> a pod:Token , pod:ConsumerToken ;\n"
            '    dcterms:title "colleagues" ;\n'
            "    pod:linkedView <https://pod.example/.system/views/reading-list> ;\n"
            "    pod:policyRef <https://pod.example/.system/tokens/policies/NGlxYzZa> ;\n"
            "    pod:enforcementCount 0 ;\n"
            '    pod:tokenSecret "3q2xkY…M8slQ" .',
        ),
        400: {"description": "The RDF body does not parse."},
        401: UNAUTHORIZED,
        415: {"description": "`Content-Type` is not one of the four RDF media types."},
    },
    openapi_extra={
        "security": ADMIN_AUTH,
        "requestBody": rdf_request_body(
            "An RDF description whose `pod:linkedView` objects name the views this grant "
            "unlocks (any number, including none; the subject term is irrelevant). An "
            "optional `dcterms:title` names the grant for the owner's own recognition — "
            "e.g. `access-bob` or `colleagues` — and never affects validation.",
            "@prefix pod: <urn:pod:vocab:> .\n"
            "@prefix dcterms: <http://purl.org/dc/terms/> .\n\n"
            '[] dcterms:title "colleagues" ;\n'
            "   pod:linkedView <https://pod.example/.system/views/reading-list> ,\n"
            "                  <https://pod.example/.system/views/schedule> .",
        ),
    },
)
def issue_grant(
    request: Request,
    backend: BackendDep,
    token: AdminTokenDep,
    body: RawBodyDep,
    content_type: Annotated[str | None, Header()] = None,
) -> Response:
    """Issue a grant from an RDF description of the views it unlocks.

    The response is the created record's representation plus one triple that
    exists nowhere else: ``pod:tokenSecret``, the plaintext bearer token,
    surfaced exactly once — only its SHA-256 hash is stored on the record.
    """
    tokens_ns = str(request.app.state.system_ns) + "tokens/"
    graph = parse_rdf_body(body, content_type, base_uri=tokens_ns)
    linked = sorted(str(v) for v in graph.objects(None, POD_linkedView))
    title = next(iter(graph.objects(None, DC_title)), None)
    name = str(title) if title is not None else None
    plaintext, record_uri = issue_token(backend, request.app.state.system_ns, linked, name=name)
    out = backend.read(record_uri)
    out.add((URIRef(record_uri), POD_tokenSecret, Literal(plaintext, datatype=XSD.string)))
    return Response(
        status_code=201,
        content=out.serialize(format="turtle"),
        media_type="text/turtle",
        headers={"Location": record_uri},
    )


@router.put(
    "/tokens/policies/{policy_id}",
    operation_id="writePolicy",
    summary="Create or replace a grant's policy",
    response_class=Response,
    responses={
        200: turtle_response("Replaced; the canonicalized policy as stored."),
        201: turtle_response("Created; the canonicalized policy as stored."),
        400: {"description": "The RDF body does not parse."},
        401: UNAUTHORIZED,
        415: {"description": "`Content-Type` is not one of the four RDF media types."},
        422: {
            "description": (
                "Not exactly one `pod:Policy` subject, or a constraint value outside its "
                "datatype (`xsd:dateTime` / `xsd:integer`)."
            )
        },
    },
    openapi_extra={
        "security": ADMIN_AUTH,
        "requestBody": rdf_request_body(
            "One `pod:Policy` subject carrying any subset of the five constraints: "
            "`pod:expiresAt`, `pod:validFrom`, `pod:validUntil` (`xsd:dateTime`); "
            "`pod:maxRetrievals` (total deliveries for the grant) and `pod:minInterval` "
            "(seconds between deliveries, `xsd:integer`). Omitted constraints do not "
            "bind. The `{policy_id}` to PUT to is the last path segment of the grant's "
            "`pod:policyRef`.",
            "@prefix pod: <urn:pod:vocab:> .\n"
            "@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .\n\n"
            "[] a pod:Policy ;\n"
            '    pod:expiresAt "2026-12-31T23:59:59Z"^^xsd:dateTime ;\n'
            '    pod:maxRetrievals "20"^^xsd:integer ;\n'
            '    pod:minInterval "60"^^xsd:integer .',
        ),
    },
)
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
    policy_uri = str(request.app.state.system_ns) + "tokens/policies/" + policy_id
    graph = parse_rdf_body(body, content_type, base_uri=policy_uri)
    subjects = list(graph.subjects(RDF.type, POD_Policy))
    if len(subjects) != 1:
        raise HTTPException(
            status_code=422, detail="Body must describe exactly one pod:Policy resource"
        )
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
        # A record only counts as a member when it both carries the container's
        # rdf:type marker and lives under the container's URI subtree. The prefix
        # guard keeps a stray typed subject elsewhere in the union graph — e.g. a
        # public data resource typed pod:View, or a relative IRI that resolved
        # outside the pod — from leaking in as a phantom member.
        prefix = container_uri
        for row in backend.query(_CONTAINER_MEMBER_QUERIES[key], include_system=True).bindings:
            member = row.get(Variable("m"))
            if member is not None and str(member).startswith(prefix):
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


@router.get(
    "/{path:path}",
    operation_id="readSystemResource",
    summary="Browse the management tree",
    description=(
        "Read a management record or one of the synthesized LDP Basic Containers that "
        "list them: `` ``(the `.system/` root), `views/`, `tokens/`, `tokens/policies/`, "
        "and `access-log/`. Container listings are derived live from the records' "
        "`rdf:type` markers, so they are always current. Responses are `text/turtle`. "
        "Token records never contain the plaintext secret — only its hash. The engine's "
        "credential can read `views/` and `tokens/` only; the admin token reads everything."
    ),
    response_class=Response,
    responses={
        200: turtle_response(
            "The record or container listing.",
            "@prefix ldp: <http://www.w3.org/ns/ldp#> .\n\n"
            "<https://pod.example/.system/views/> a ldp:BasicContainer ;\n"
            "    ldp:contains <https://pod.example/.system/views/reading-list> ,\n"
            "                 <https://pod.example/.system/views/schedule> .",
        ),
        401: UNAUTHORIZED,
        403: {"description": "The engine credential asked for a record kind outside its scope."},
        404: {"description": "No record at this path."},
    },
    openapi_extra={"security": STORAGE_AUTH},
)
def read_system(
    path: str, backend: BackendDep, settings: SettingsDep, token: StorageTokenDep
) -> Response:
    if token.token_type == str(POD_EngineToken) and not path.startswith(_ENGINE_READABLE_PREFIXES):
        raise HTTPException(status_code=403)
    key = path.rstrip("/")
    if key in _CONTAINER_MEMBER_QUERIES or path == "":
        return _synthesize_container(key, backend, settings)
    uri = settings.base_uri + ".system/" + path
    try:
        graph = backend.read(uri)
    except StorageError as exc:
        raise http_error(exc) from exc
    # The ETag lets the engine read-modify-write a record's enforcement fields with a
    # conditional PUT (If-Match); it is the same digest the write path validates against.
    return Response(
        content=graph.serialize(format="turtle"),
        media_type="text/turtle",
        headers={"ETag": etag_for_graph(graph)},
    )


@router.delete(
    "/{path:path}",
    status_code=204,
    operation_id="revokeSystemResource",
    summary="Revoke a grant or delete a management record",
    description=(
        "Delete the record at this path. Deleting a token record (`tokens/{id}`) revokes "
        "the grant instantly: the very next request bearing that token — consumer or "
        "engine — fails with the uniform 401."
    ),
    response_class=Response,
    responses={
        204: {"description": "Deleted; a revoked token is dead immediately."},
        401: UNAUTHORIZED,
        404: {"description": "No record at this path."},
    },
    openapi_extra={"security": ADMIN_AUTH},
)
def revoke_system(
    path: str, backend: BackendDep, settings: SettingsDep, token: AdminTokenDep
) -> Response:
    uri = settings.base_uri + ".system/" + path
    try:
        revoke_token(backend, uri)
    except StorageError as exc:
        raise http_error(exc) from exc
    return Response(status_code=204)
