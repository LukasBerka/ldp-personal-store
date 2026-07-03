"""Append-only access log for confirmed view deliveries.

Each successful delivery records one immutable entry under ``.system/access-log/``:
which view was served, which token authorized it, and when. Entries are written as
independent resources (a fresh id per call) so appending is a plain single write with
no read-modify-write, and the statistics layer aggregates them with a single SPARQL
query over the union graph.
"""

import secrets

from rdflib import Graph, Literal, Namespace, URIRef
from rdflib.namespace import RDF, XSD

from app.storage.backend import StorageBackend
from app.vocab import (
    POD_AccessLogEntry,
    POD_accessLogTimestamp,
    POD_accessLogToken,
    POD_accessLogView,
)


def append_access_log_entry(
    backend: StorageBackend,
    system_ns: Namespace,
    view_uri: str,
    token_uri: str,
    timestamp: str,
) -> None:
    """Write one access-log entry recording a confirmed delivery.

    *timestamp* is the request instant already computed for the enforcement bump; it is
    passed in rather than read here so the log and the counters agree and there is exactly
    one clock read per request.
    """
    entry_uri = str(system_ns) + "access-log/" + secrets.token_urlsafe(8)
    subject = URIRef(entry_uri)
    graph = Graph()
    graph.add((subject, RDF.type, POD_AccessLogEntry))
    graph.add((subject, POD_accessLogView, URIRef(view_uri)))
    graph.add((subject, POD_accessLogToken, URIRef(token_uri)))
    graph.add((subject, POD_accessLogTimestamp, Literal(timestamp, datatype=XSD.dateTime)))
    backend.write_system(entry_uri, graph)
