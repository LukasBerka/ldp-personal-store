"""Filesystem-backed :class:`StorageBackend` over an in-memory rdflib graph.

RDF resources are Turtle files; binary resources are raw bytes plus a Turtle
metadata sidecar. On construction the backend walks ``storage_root`` and rebuilds
a :class:`~rdflib.ConjunctiveGraph` holding one named graph per resource URI, so a
fresh instance reconstructs the same state from disk. Disk stays authoritative:
each write/delete updates the named graph incrementally rather than rebuilding.
"""

import re
import threading
from collections.abc import Iterator
from pathlib import Path

from rdflib import ConjunctiveGraph, Graph, Literal, URIRef
from rdflib.namespace import RDF, XSD
from rdflib.query import Result

from app.storage.backend import NotABinaryResource, ResourceNotFound, StorageError
from app.storage.system import assert_public_uri, ensure_system_subtree
from app.vocab import (
    DC_format,
    LDP_NonRDFSource,
    POD_enforcementCount,
    POD_lastUsedAt,
    POD_viewRetrievalCount,
)

_IRI_SCHEME = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*:")


def _to_term(value: str) -> URIRef | Literal:
    # init_bindings values arrive as plain strings; rdflib will not coerce them,
    # so absolute-IRI-looking values become URIRefs and everything else a Literal.
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


def _meta_path_to_uri(path: Path, storage_root: Path, base_uri: str) -> str:
    # A binary sidecar foo.png.meta.ttl describes the binary resource foo.png, so
    # the whole ".meta.ttl" suffix is stripped; stripping only ".ttl" (as
    # _path_to_uri does) would leave a stray ".meta" and point at no resource.
    segment = path.relative_to(storage_root).as_posix().removesuffix(".meta.ttl")
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
        # ConjunctiveGraph is not thread-safe; this guards every read-modify-write
        # so concurrent threadpool requests cannot corrupt the in-memory store.
        self._lock = threading.RLock()
        self._graph = ConjunctiveGraph()
        storage_root.mkdir(parents=True, exist_ok=True)
        ensure_system_subtree(storage_root)
        with self._lock:
            for path in storage_root.rglob("*.ttl"):
                # Binary sidecars (foo.png.meta.ttl) carry the binary's RDF
                # metadata; loading them too lets a restarted backend rebuild a
                # SPARQL-discoverable graph identical to before restart, not just
                # the RDF resources. Raw binary bytes stay on disk, never in the graph.
                if path.name.endswith(".meta.ttl"):
                    uri = _meta_path_to_uri(path, storage_root, base_uri)
                else:
                    uri = _path_to_uri(path, storage_root, base_uri)
                context = self._graph.get_context(URIRef(uri))
                context.parse(data=path.read_text(encoding="utf-8"), format="turtle")

    def read(self, uri: str) -> Graph:
        path = _guard_within_root(
            _uri_to_rdf_path(uri, self._storage_root, self._base_uri), self._storage_root, uri
        )
        result = Graph()
        with self._lock:
            if not path.exists():
                raise ResourceNotFound(uri)
            # Copy out of the named-graph context so callers never hold a live
            # reference into the shared ConjunctiveGraph store.
            for triple in self._graph.get_context(URIRef(uri)):
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
            self._graph.remove_context(self._graph.get_context(URIRef(uri)))
            self._graph.get_context(URIRef(uri)).parse(
                data=path.read_text(encoding="utf-8"), format="turtle"
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
        with self._lock:
            bin_path.parent.mkdir(parents=True, exist_ok=True)
            # Raw bytes live only on disk; the graph holds the metadata sidecar.
            bin_path.write_bytes(data)
            meta_path.write_text(serialized, encoding="utf-8")
            self._graph.remove_context(self._graph.get_context(subject))
            self._graph.get_context(subject).parse(
                data=meta_path.read_text(encoding="utf-8"), format="turtle"
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
            elif bin_path.exists():
                bin_path.unlink()
                meta_path.unlink(missing_ok=True)
            else:
                raise ResourceNotFound(uri)
            self._graph.remove_context(self._graph.get_context(URIRef(uri)))

    def update_enforcement(self, uri: str, count: int, last_used_at: str) -> None:
        subject = URIRef(uri)
        with self._lock:
            # Copy the record's named graph, then rewrite only the two mutable
            # fields — every other triple (e.g. the token hash) is carried through
            # untouched. write_system re-acquires _lock, which the reentrant RLock
            # permits, so the whole read-modify-write stays a single atomic section.
            graph = Graph()
            for triple in self._graph.get_context(subject):
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
            for triple in self._graph.get_context(subject):
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

    def query(self, sparql: str, init_bindings: dict[str, str] | None = None) -> Result:
        bindings = None
        if init_bindings is not None:
            bindings = {var: _to_term(value) for var, value in init_bindings.items()}
        # ConjunctiveGraph queries the union of all named graphs, so a plain
        # `?s ?p ?o` pattern sees every resource without a GRAPH wrapper.
        with self._lock:
            return self._graph.query(sparql, initBindings=bindings)
