"""Shared startup helper used by both the storage-role app and the bundled entrypoint."""

from rdflib import Graph, URIRef
from rdflib.namespace import RDF

from ldp_common.vocab import LDP_BasicContainer, LDP_RDFSource, LDP_Resource
from ldp_personal_store.storage.backend import ResourceNotFound, StorageBackend


def init_root_container(backend: StorageBackend, base_uri: str) -> None:
    """Seed the pod root as an empty Basic Container on first startup.

    The root URI is not under the reserved ``.system/`` subtree, so the public
    write path accepts it.
    """
    try:
        backend.read(base_uri)
    except ResourceNotFound:
        root = URIRef(base_uri)
        graph = Graph()
        graph.add((root, RDF.type, LDP_Resource))
        graph.add((root, RDF.type, LDP_RDFSource))
        graph.add((root, RDF.type, LDP_BasicContainer))
        backend.write(base_uri, graph)
