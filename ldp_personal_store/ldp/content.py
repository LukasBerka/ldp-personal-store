"""Storage-coupled content helper. The pure RDF/ETag/negotiation helpers live in
:mod:`ldp_common.rdfcontent`; only this one needs a storage backend.
"""

from rdflib.term import Variable

from ldp_common.vocabulary import DC_format
from ldp_personal_store.storage.backend import StorageBackend


def binary_content_type(backend: StorageBackend, uri: str) -> str:
    """Return the stored media type for the binary resource at *uri*.

    Binary metadata lives in a sidecar graph that ``read`` does not expose, so the
    media type is fetched by querying its ``dcterms:format`` literal.
    """
    result = backend.query(
        f"SELECT ?ct WHERE {{ ?s <{DC_format}> ?ct }}",
        init_bindings={"s": uri},
    )
    for row in result.bindings:
        value = row.get(Variable("ct"))
        if value is not None:
            return str(value)
    return "application/octet-stream"
