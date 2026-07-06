from fastapi.testclient import TestClient
from rdflib import Graph, URIRef
from rdflib.compare import isomorphic

from tests.support import (
    TURTLE,
    bearer,
    create_view,
    issue_consumer_token,
    policy_id_from_record_uri,
    put_policy,
)

_OWNED = b'<http://test.localhost/alice> <http://example.org/name> "Alice" .'
# A constant CONSTRUCT with an empty WHERE emits a fixed triple regardless of pod
# contents, so the delivered slice is deterministic and can be asserted exactly.
_SLICE_TEMPLATE = "CONSTRUCT { <urn:ex:alice> <urn:ex:knows> <urn:ex:bob> } WHERE {}"
_SLICE_TRIPLE = (
    URIRef("urn:ex:alice"),
    URIRef("urn:ex:knows"),
    URIRef("urn:ex:bob"),
)

# One public and one private resource, so a filtering view can be shown to deliver
# the public projection and withhold the private resource entirely.
_PUBLIC = (
    b'<http://test.localhost/alice> <http://example.org/visibility> "public" .\n'
    b'<http://test.localhost/alice> <http://example.org/name> "Alice Public" .'
)
_PRIVATE = (
    b'<http://test.localhost/secret> <http://example.org/visibility> "private" .\n'
    b'<http://test.localhost/secret> <http://example.org/ssn> "999-99-9999" .'
)
_FILTER_TEMPLATE = (
    "CONSTRUCT { ?s <http://example.org/name> ?n } "
    'WHERE { ?s <http://example.org/visibility> "public" ; <http://example.org/name> ?n }'
)


def _record_id(record_uri: str) -> str:
    return record_uri.rsplit("/", 1)[-1]


def _graph(text: str) -> Graph:
    graph = Graph()
    graph.parse(data=text, format="turtle")
    return graph


def test_owner_stores_and_reads_back_data(client: TestClient, admin_token: str) -> None:
    """The owner PUTs an RDF resource into the pod and reads back the same graph."""
    created = client.put("/alice", content=_OWNED, headers={**bearer(admin_token), **TURTLE})
    assert created.status_code == 201, created.text

    fetched = client.get("/alice", headers=bearer(admin_token))
    assert fetched.status_code == 200
    assert isomorphic(_graph(fetched.text), _graph(_OWNED.decode()))


def test_owner_shares_view_slice_with_consumer(client: TestClient, admin_token: str) -> None:
    view_uri, view_id = create_view(client, admin_token, template=_SLICE_TEMPLATE)
    token, _ = issue_consumer_token(client, admin_token, linked_view_uri=view_uri)

    delivered = client.get(f"/.engine/views/{view_id}", headers=bearer(token))
    assert delivered.status_code == 200

    expected = Graph()
    expected.add(_SLICE_TRIPLE)
    assert isomorphic(_graph(delivered.text), expected)


def test_view_exposes_only_the_selected_subset(client: TestClient, admin_token: str) -> None:
    owner = {**bearer(admin_token), **TURTLE}
    client.put("/alice", content=_PUBLIC, headers=owner)
    client.put("/secret", content=_PRIVATE, headers=owner)

    view_uri, view_id = create_view(client, admin_token, template=_FILTER_TEMPLATE)
    token, _ = issue_consumer_token(client, admin_token, linked_view_uri=view_uri)

    delivered = client.get(f"/.engine/views/{view_id}", headers=bearer(token))
    assert delivered.status_code == 200

    slice_graph = _graph(delivered.text)
    # Exactly the public projection: the one selected triple and nothing else.
    assert len(slice_graph) == 1
    assert "Alice Public" in {str(obj) for _, _, obj in slice_graph}

    # The private resource never leaks — not its value, its marker, nor its name.
    assert "999-99-9999" not in delivered.text
    assert "secret" not in delivered.text
    assert "private" not in delivered.text


def test_consumer_without_valid_token_denied(client: TestClient, admin_token: str) -> None:
    _, view_id = create_view(client, admin_token, template=_SLICE_TEMPLATE)

    missing = client.get(f"/.engine/views/{view_id}")
    assert missing.status_code == 401
    assert missing.headers["WWW-Authenticate"] == "Bearer"

    garbage = client.get(f"/.engine/views/{view_id}", headers=bearer("not-a-real-token"))
    assert garbage.status_code == 401
    assert garbage.headers["WWW-Authenticate"] == "Bearer"


def test_consumer_token_confined_to_its_slice(client: TestClient, admin_token: str) -> None:
    client.put("/alice", content=_OWNED, headers={**bearer(admin_token), **TURTLE})
    granted_uri, granted_id = create_view(client, admin_token, template=_SLICE_TEMPLATE)
    _, other_id = create_view(client, admin_token, template=_SLICE_TEMPLATE)
    token, _ = issue_consumer_token(client, admin_token, linked_view_uri=granted_uri)

    assert client.get(f"/.engine/views/{granted_id}", headers=bearer(token)).status_code == 200

    # A view the grant does not link is outside its scope.
    assert client.get(f"/.engine/views/{other_id}", headers=bearer(token)).status_code == 403

    # The consumer credential cannot reach the raw store or the query endpoint at all.
    assert client.get("/alice", headers=bearer(token)).status_code == 401
    query = {"query": "ASK { ?s ?p ?o }"}
    assert client.get("/sparql", params=query, headers=bearer(token)).status_code == 401


def test_revoked_token_denied(client: TestClient, admin_token: str) -> None:
    view_uri, view_id = create_view(client, admin_token, template=_SLICE_TEMPLATE)
    token, record_uri = issue_consumer_token(client, admin_token, linked_view_uri=view_uri)

    assert client.get(f"/.engine/views/{view_id}", headers=bearer(token)).status_code == 200

    revoked = client.delete(
        f"/.system/tokens/{_record_id(record_uri)}", headers=bearer(admin_token)
    )
    assert revoked.status_code == 204

    assert client.get(f"/.engine/views/{view_id}", headers=bearer(token)).status_code == 401


def test_policy_limit_denies_past_bound(client: TestClient, admin_token: str) -> None:
    view_uri, view_id = create_view(client, admin_token, template=_SLICE_TEMPLATE)
    token, record_uri = issue_consumer_token(client, admin_token, linked_view_uri=view_uri)

    bounded = put_policy(
        client, admin_token, policy_id_from_record_uri(record_uri), max_retrievals=1
    )
    assert bounded.status_code in (200, 201), bounded.text

    assert client.get(f"/.engine/views/{view_id}", headers=bearer(token)).status_code == 200
    assert client.get(f"/.engine/views/{view_id}", headers=bearer(token)).status_code == 403
