from fastapi.testclient import TestClient
from httpx import Response
from rdflib import BNode, Graph, Literal, URIRef
from rdflib.namespace import RDF, XSD
from rdflib.term import Node

from ldp_common.vocabulary import (
    DC_description,
    DC_title,
    POD_constructTemplate,
    POD_contentTypeHint,
    POD_expiresAt,
    POD_linkedView,
    POD_maxRetrievals,
    POD_maxViewRetrievals,
    POD_minInterval,
    POD_parameter,
    POD_paramName,
    POD_paramType,
    POD_Policy,
    POD_Token,
    POD_tokenSecret,
    POD_validFrom,
    POD_validUntil,
    POD_View,
)

BASE = "http://test.localhost/"
ADMIN_TOKEN = "test-suite-admin-token"

TURTLE = {"Content-Type": "text/turtle"}

# A constant CONSTRUCT with no WHERE bindings keeps view responses deterministic
# regardless of pod contents, so authoring tests never depend on stored data.
DEFAULT_TEMPLATE = "CONSTRUCT { <urn:ex:a> <urn:ex:b> <urn:ex:c> } WHERE {}"


def bearer(token: str, extra: dict[str, str] | None = None) -> dict[str, str]:
    """Bearer authorization header for a presented opaque token, merged with *extra*."""
    headers = {"Authorization": f"Bearer {token}"}
    if extra:
        headers.update(extra)
    return headers


def system_path(uri: str) -> str:
    """TestClient request path for a stored ``.system`` URI (leading-slash absolute)."""
    return "/" + uri.removeprefix(BASE)


def create_view(
    client: TestClient,
    admin_token: str,
    *,
    title: str = "test view",
    template: str = DEFAULT_TEMPLATE,
    content_type_hint: str = "text/turtle",
    params: list[dict[str, str]] | None = None,
    max_view_retrievals: int | None = None,
) -> tuple[str, str]:
    """POST a view definition as Turtle and return its ``(view_uri, view_id)``."""
    response = client.post(
        "/.system/views",
        content=view_turtle(
            title,
            template,
            content_type_hint=content_type_hint,
            params=params,
            max_view_retrievals=max_view_retrievals,
        ),
        headers={**bearer(admin_token), **TURTLE},
    )
    assert response.status_code == 201, response.text
    view_uri = response.headers["Location"]
    return view_uri, view_uri.rsplit("/", 1)[-1]


def issue_consumer_token(
    client: TestClient, admin_token: str, linked_view_uri: str | None = None
) -> tuple[str, str]:
    """Issue a consumer token over HTTP and return its ``(plaintext, record_uri)``."""
    response = client.post(
        "/.system/tokens",
        content=token_turtle([linked_view_uri] if linked_view_uri else []),
        headers={**bearer(admin_token), **TURTLE},
    )
    assert response.status_code == 201, response.text
    record_uri = response.headers["Location"]
    return token_secret(record_uri, response.text), record_uri


def put_policy(
    client: TestClient, admin_token: str, policy_id: str, **constraints: object
) -> Response:
    """PUT the provided constraints as Turtle to a policy URI and return the response."""
    return client.put(
        f"/.system/tokens/policies/{policy_id}",
        content=policy_turtle(**constraints),
        headers={**bearer(admin_token), **TURTLE},
    )


def policy_id_from_record_uri(record_uri: str) -> str:
    """The policy URI is 1:1 with the record id, so the trailing segment is the policy id."""
    return record_uri.rsplit("/", 1)[-1]


def read_int_property(
    client: TestClient, admin_token: str, uri: str, predicate: Node
) -> int | None:
    """Read a single integer property of a ``.system`` resource back over HTTP."""
    response = client.get(system_path(uri), headers=bearer(admin_token))
    if response.status_code != 200:
        return None
    graph = Graph()
    graph.parse(data=response.text, format="turtle")
    value = graph.value(URIRef(uri), URIRef(str(predicate)))
    return int(str(value)) if value is not None else None


_POLICY_CONSTRAINT_PROPS = {
    "expires_at": (POD_expiresAt, XSD.dateTime),
    "valid_from": (POD_validFrom, XSD.dateTime),
    "valid_until": (POD_validUntil, XSD.dateTime),
    "max_retrievals": (POD_maxRetrievals, XSD.integer),
    "min_interval": (POD_minInterval, XSD.integer),
}


def view_turtle(
    title: str,
    template: str,
    content_type_hint: str | None = "text/turtle",
    params: list[dict[str, str]] | None = None,
    max_view_retrievals: int | None = None,
    description: str = "",
) -> str:
    """Turtle body describing one ``pod:View`` for POST/PUT on ``/.system/views``."""
    graph = Graph()
    subject = URIRef("urn:submission:view")
    graph.add((subject, RDF.type, POD_View))
    graph.add((subject, DC_title, Literal(title)))
    if description:
        graph.add((subject, DC_description, Literal(description)))
    graph.add((subject, POD_constructTemplate, Literal(template)))
    if content_type_hint is not None:
        graph.add((subject, POD_contentTypeHint, Literal(content_type_hint)))
    for param in params or []:
        pnode = BNode()
        graph.add((subject, POD_parameter, pnode))
        graph.add((pnode, POD_paramName, Literal(param["name"])))
        graph.add((pnode, POD_paramType, Literal(param["type"])))
    if max_view_retrievals is not None:
        graph.add((subject, POD_maxViewRetrievals, Literal(max_view_retrievals)))
    return graph.serialize(format="turtle")


def token_turtle(linked_view_uris: list[str], name: str | None = None) -> str:
    """Turtle body describing the grant to issue via POST on ``/.system/tokens``."""
    graph = Graph()
    subject = URIRef("urn:submission:token")
    graph.add((subject, RDF.type, POD_Token))
    if name is not None:
        graph.add((subject, DC_title, Literal(name)))
    for view_uri in linked_view_uris:
        graph.add((subject, POD_linkedView, URIRef(view_uri)))
    return graph.serialize(format="turtle")


def policy_turtle(**constraints: object) -> str:
    """Turtle body describing one ``pod:Policy`` for PUT on its policy URI."""
    graph = Graph()
    subject = URIRef("urn:submission:policy")
    graph.add((subject, RDF.type, POD_Policy))
    for name, value in constraints.items():
        prop, datatype = _POLICY_CONSTRAINT_PROPS[name]
        graph.add((subject, prop, Literal(str(value), datatype=datatype)))
    return graph.serialize(format="turtle")


def token_secret(record_uri: str, response_text: str) -> str:
    """Extract the one-time ``pod:tokenSecret`` plaintext from an issuance response body."""
    graph = Graph()
    graph.parse(data=response_text, format="turtle")
    secret = graph.value(URIRef(record_uri), POD_tokenSecret)
    assert secret is not None, "issuance response carried no pod:tokenSecret"
    return str(secret)
