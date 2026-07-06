"""Filesystem-backed :class:`StorageBackend` over an in-memory rdflib dataset.

RDF resources are Turtle files; binary resources are raw bytes plus a Turtle
metadata sidecar. On construction the backend walks ``storage_root`` and rebuilds
a :class:`~rdflib.Dataset` holding one named graph per resource URI, so a fresh
instance reconstructs the same state from disk. Disk stays authoritative: each
write/delete updates the named graph incrementally rather than rebuilding.
"""

import re
import threading
from collections.abc import Iterator
from pathlib import Path

from rdflib import Dataset, Graph, Literal, URIRef
from rdflib.graph import ReadOnlyGraphAggregate
from rdflib.namespace import RDF, XSD
from rdflib.query import Result

from ldp_personal_store.storage.backend import NotABinaryResource, ResourceNotFound, StorageError
from ldp_personal_store.storage.system import (
    SYSTEM_SEGMENT,
    assert_public_uri,
    ensure_system_subtree,
)
from ldp_personal_store.vocab import (
    DC_format,
    LDP_NonRDFSource,
    POD_enforcementCount,
    POD_lastUsedAt,
    POD_viewRetrievalCount,
)

_IRI_SCHEME = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*:")


def _to_term(value: str, datatype: str | None = None) -> URIRef | Literal:
    # init_bindings values arrive as plain strings; rdflib will not coerce them,
    # so absolute-IRI-looking values become URIRefs and everything else a Literal.
    # An explicit datatype (carried by the bindingtype-<name> protocol field) binds
    # a typed literal instead — the only way to compare a parameter against typed
    # date literals, since rdflib cannot cast a plain literal to a date in-query.
    if datatype is not None:
        return Literal(value, datatype=URIRef(datatype))
    if _IRI_SCHEME.match(value):
        return URIRef(value)
    return Literal(value)


def _uri_to_rdf_path(uri: str, storage_root: Path, base_uri: str) -> Path:
    relative = uri.removeprefix(base_uri)
    return storage_root / f"{relative}.ttl"


def _uri_to_bin_path(uri: str, storage_root: Path, base_uri: str) -> Path:
    relative = uri.removeprefix(base_uri)
    return storage_root / relative


def _path_to_uri(path: Path, storage_root: Path, base_uri: str) -> str:
    # as_posix keeps URIs slash-separated regardless of the host OS path separator.
    segment = path.relative_to(storage_root).as_posix().removesuffix(".ttl")
    return base_uri + segment


def _guard_within_root(candidate: Path, storage_root: Path, uri: str) -> Path:
    """Resolve *candidate* and reject any path escaping *storage_root*.

    Stripping the base-URI prefix is not enough: the remainder may contain ``..``
    segments, so every filesystem operation must route through this chokepoint.
    """
    resolved = candidate.resolve()
    if not resolved.is_relative_to(storage_root.resolve()):
        raise StorageError(f"URI maps outside storage root: {uri!r}")
    return resolved


class FilesystemBackend:
    def __init__(self, storage_root: Path, base_uri: str) -> None:
        self._storage_root = storage_root
        self._base_uri = base_uri
        self._system_prefix = base_uri.rstrip("/") + "/" + SYSTEM_SEGMENT + "/"
        # The Dataset is not thread-safe; this guards every read-modify-write
        # so concurrent threadpool requests cannot corrupt the in-memory store.
        self._lock = threading.RLock()
        # default_union makes queries see the union of all named graphs, so a
        # plain `?s ?p ?o` pattern reaches every resource without a GRAPH wrapper.
        self._graph = Dataset(default_union=True)
        storage_root.mkdir(parents=True, exist_ok=True)
        ensure_system_subtree(storage_root)
        with self._lock:
            for path in storage_root.rglob("*.ttl"):
                # A binary sidecar foo.png.meta.ttl loads like any other Turtle file,
                # so its named graph is keyed by the description URI foo.png.meta —
                # the URI the LDP-NR's describedby link resolves to. This rebuilds a
                # SPARQL-discoverable graph identical to before restart; raw binary
                # bytes stay on disk, never in the graph.
                uri = _path_to_uri(path, storage_root, base_uri)
                context = self._graph.graph(URIRef(uri))
                context.parse(data=path.read_text(encoding="utf-8"), format="turtle", publicID=uri)

    def read(self, uri: str) -> Graph:
        path = _guard_within_root(
            _uri_to_rdf_path(uri, self._storage_root, self._base_uri), self._storage_root, uri
        )
        result = Graph()
        with self._lock:
            if not path.exists():
                raise ResourceNotFound(uri)
            # Copy out of the named-graph context so callers never hold a live
            # reference into the shared Dataset store.
            for triple in self._graph.graph(URIRef(uri)):
                result.add(triple)
        return result

    def write(self, uri: str, graph: Graph) -> None:
        assert_public_uri(uri, self._base_uri)
        self.write_system(uri, graph)

    def write_system(self, uri: str, graph: Graph) -> None:
        # Bypasses the public prefix check so server-managed .system/ writes succeed;
        # public callers reach persistence only through write()/write_binary().
        path = _guard_within_root(
            _uri_to_rdf_path(uri, self._storage_root, self._base_uri), self._storage_root, uri
        )
        serialized = graph.serialize(format="turtle")
        with self._lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(serialized, encoding="utf-8")
            self._graph.remove_graph(self._graph.graph(URIRef(uri)))
            self._graph.graph(URIRef(uri)).parse(
                data=path.read_text(encoding="utf-8"), format="turtle", publicID=uri
            )

    def write_binary(self, uri: str, data: bytes, content_type: str) -> None:
        assert_public_uri(uri, self._base_uri)
        bin_path = _guard_within_root(
            _uri_to_bin_path(uri, self._storage_root, self._base_uri), self._storage_root, uri
        )
        subject = URIRef(uri)
        meta = Graph()
        meta.add((subject, RDF.type, LDP_NonRDFSource))
        meta.add((subject, DC_format, Literal(content_type)))
        serialized = meta.serialize(format="turtle")
        meta_path = Path(f"{bin_path}.meta.ttl")
        # The sidecar is addressable as the LDP-NR's description (LDP-RS) resource at
        # "{uri}.meta" — the target of the describedby link. Key its named graph by
        # that description URI (the same URI init derives from the sidecar path), so a
        # GET of the describedby target resolves it. The triples still describe the
        # binary itself (subject = uri).
        description = URIRef(f"{uri}.meta")
        with self._lock:
            bin_path.parent.mkdir(parents=True, exist_ok=True)
            # Raw bytes live only on disk; the graph holds the metadata sidecar.
            bin_path.write_bytes(data)
            meta_path.write_text(serialized, encoding="utf-8")
            self._graph.remove_graph(self._graph.graph(description))
            self._graph.graph(description).parse(
                data=meta_path.read_text(encoding="utf-8"), format="turtle", publicID=uri
            )

    def delete(self, uri: str) -> None:
        # Guard first: a .system/ URI must raise before any path resolution or
        # disk mutation, so public LDP DELETE can never remove server-managed records.
        assert_public_uri(uri, self._base_uri)
        self.delete_system(uri)

    def delete_system(self, uri: str) -> None:
        # No prefix guard: the internal revocation path removes .system/ records
        # that delete() refuses to touch.
        rdf_path = _guard_within_root(
            _uri_to_rdf_path(uri, self._storage_root, self._base_uri), self._storage_root, uri
        )
        bin_path = _guard_within_root(
            _uri_to_bin_path(uri, self._storage_root, self._base_uri), self._storage_root, uri
        )
        meta_path = Path(f"{bin_path}.meta.ttl")
        with self._lock:
            if rdf_path.exists():
                rdf_path.unlink()
                self._graph.remove_graph(self._graph.graph(URIRef(uri)))
            elif bin_path.exists():
                # Deleting an LDP-NR also removes its description resource, whose
                # named graph is keyed at "{uri}.meta".
                bin_path.unlink()
                meta_path.unlink(missing_ok=True)
                self._graph.remove_graph(self._graph.graph(URIRef(f"{uri}.meta")))
            else:
                raise ResourceNotFound(uri)

    def update_enforcement(self, uri: str, count: int, last_used_at: str) -> None:
        subject = URIRef(uri)
        with self._lock:
            # Copy the record's named graph, then rewrite only the two mutable
            # fields — every other triple (e.g. the token hash) is carried through
            # untouched. write_system re-acquires _lock, which the reentrant RLock
            # permits, so the whole read-modify-write stays a single atomic section.
            graph = Graph()
            for triple in self._graph.graph(subject):
                graph.add(triple)
            graph.remove((subject, POD_enforcementCount, None))
            graph.remove((subject, POD_lastUsedAt, None))
            graph.add((subject, POD_enforcementCount, Literal(count, datatype=XSD.integer)))
            graph.add((subject, POD_lastUsedAt, Literal(last_used_at, datatype=XSD.dateTime)))
            self.write_system(uri, graph)

    def update_view_enforcement(self, view_uri: str, count: int) -> None:
        subject = URIRef(view_uri)
        with self._lock:
            # Copy the view record's named graph, then rewrite only the counter — every
            # other triple is carried through untouched. write_system re-acquires _lock,
            # which the reentrant RLock permits, so the whole read-modify-write stays a
            # single atomic section. A record lacking the counter is handled naturally:
            # remove is a no-op and the triple is created.
            graph = Graph()
            for triple in self._graph.graph(subject):
                graph.add(triple)
            graph.remove((subject, POD_viewRetrievalCount, None))
            graph.add((subject, POD_viewRetrievalCount, Literal(count, datatype=XSD.integer)))
            self.write_system(view_uri, graph)

    def stream_binary(self, uri: str, chunk_size: int = 65536) -> Iterator[bytes]:
        bin_path = _guard_within_root(
            _uri_to_bin_path(uri, self._storage_root, self._base_uri), self._storage_root, uri
        )
        if not bin_path.is_file():
            rdf_path = _uri_to_rdf_path(uri, self._storage_root, self._base_uri)
            if rdf_path.exists():
                raise NotABinaryResource(uri)
            raise ResourceNotFound(uri)
        # Stream lazily so large files never load fully into memory.
        with bin_path.open("rb") as f:
            while chunk := f.read(chunk_size):
                yield chunk

    def query(
        self,
        sparql: str,
        init_bindings: dict[str, str] | None = None,
        include_system: bool = False,
        init_binding_types: dict[str, str] | None = None,
    ) -> Result:
        bindings = None
        if init_bindings is not None:
            types = init_binding_types or {}
            bindings = {
                var: _to_term(value, types.get(var)) for var, value in init_bindings.items()
            }
        # The Dataset was built with default_union, so a plain `?s ?p ?o` pattern
        # sees every resource without a GRAPH wrapper.
        with self._lock:
            if include_system:
                return self._graph.query(sparql, initBindings=bindings)
            # Default scope: an aggregate of every named graph outside .system/, so
            # server-managed records (token hashes, policies, the access log) stay
            # invisible unless a caller explicitly opts in. The aggregate is backed
            # by a throwaway store, so it carries no named-graph axis — GRAPH
            # clauses match nothing in this scope.
            public = [
                graph
                for graph in self._graph.graphs()
                if not str(graph.identifier).startswith(self._system_prefix)
            ]
            scope: Graph = ReadOnlyGraphAggregate(public) if public else Graph()
            return scope.query(sparql, initBindings=bindings)
