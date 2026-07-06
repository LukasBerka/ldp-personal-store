"""OpenAPI documentation metadata shared by the HTTP routers.
"""

from typing import Any

type Responses = dict[int | str, dict[str, Any]]

RDF_MEDIA_TYPES: tuple[str, ...] = (
    "text/turtle",
    "application/ld+json",
    "application/n-triples",
    "application/rdf+xml",
)

ADMIN_AUTH = [{"AdminToken": []}]
STORAGE_AUTH = [{"AdminToken": []}, {"EngineToken": []}]
CONSUMER_AUTH = [{"ConsumerToken": []}]

SECURITY_SCHEMES = {
    "AdminToken": {
        "type": "http",
        "scheme": "bearer",
        "description": (
            "The pod owner's administrative bearer token, supplied at startup via the "
            "required `LDP_ADMIN_TOKEN` environment variable; only its SHA-256 hash is "
            "stored and the plaintext is never written to the server log. Authorizes the "
            "full surface: every data read and write, the `.system/` management tree, the "
            "SPARQL endpoint, and `/.engine/stats`."
        ),
    },
    "EngineToken": {
        "type": "http",
        "scheme": "bearer",
        "description": (
            "The view engine's internal credential for the storage surface, bootstrapped "
            "at startup. Grants reads plus the three enforcement-write endpoints. Never "
            "held by end users; a frontend client has no use for it."
        ),
    },
    "ConsumerToken": {
        "type": "http",
        "scheme": "bearer",
        "description": (
            "A data consumer's grant, issued by the pod owner via `POST /.system/tokens` "
            "(the plaintext is the `pod:tokenSecret` literal in that one response) and "
            "delivered out of band. Valid only on the consumer surface: "
            "`/.engine/discovery`, `/.engine/views/{view_id}`, and "
            "`/.engine/blob/{view_id}`."
        ),
    },
}

UNAUTHORIZED = {
    "description": (
        "Authentication failed. Deliberately identical for every cause — missing header, "
        "malformed header, unknown token, revoked token, or a token of the wrong kind — "
        "so a response never reveals whether a credential exists."
    )
}


def rdf_content(example: str | None = None) -> dict:
    """A ``content`` map offering all four supported RDF serializations.

    The optional Turtle *example* documents the graph shape once; the other
    serializations carry the same triples in their own syntax.
    """
    content: dict = {media: {"schema": {"type": "string"}} for media in RDF_MEDIA_TYPES}
    if example is not None:
        content["text/turtle"]["example"] = example
    return content


def rdf_request_body(description: str, example: str) -> dict:
    return {"required": True, "description": description, "content": rdf_content(example)}


def rdf_response(description: str, example: str | None = None) -> dict:
    return {"description": description, "content": rdf_content(example)}


def turtle_response(description: str, example: str | None = None) -> dict:
    content: dict = {"text/turtle": {"schema": {"type": "string"}}}
    if example is not None:
        content["text/turtle"]["example"] = example
    return {"description": description, "content": content}
