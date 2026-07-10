"""Filesystem-backed :class:`StorageBackend` over an in-memory rdflib dataset."""

import re
import threading
from collections.abc import Callable, Iterator
from pathlib import Path

from rdflib import Dataset, Graph, Literal, URIRef
from rdflib.graph import ReadOnlyGraphAggregate
from rdflib.namespace import RDF
from rdflib.query import Result

from ldp_personal_store.storage.backend import NotABinaryResource, ResourceNotFound, StorageError
from ldp_personal_store.storage.system import (
    SYSTEM_SEGMENT,
    assert_public_uri,
    ensure_system_subtree,
)
from ldp_personal_store.vocab import DC_format, LDP_NonRDFSource

_IRI_SCHEME = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*:")


def _to_term(value: str, datatype: str | None = None) -> URIRef | Literal:
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
    """Resolve *candidate* and reject any path escaping *storage_root*."""
    resolved = candidate.resolve()
    if not resolved.is_relative_to(storage_root.resolve()):
        raise StorageError(f"URI maps outside storage root: {uri!r}")
    return resolved


class FilesystemBackend:
    def __init__(self, storage_root: Path, base_uri: str) -> None:
        self._storage_root = storage_root
        self._base_uri = base_uri
        self._system_prefix = base_uri.rstrip("/") + "/" + SYSTEM_SEGMENT + "/"
        # The Dataset is not thread-safe
        self._lock = threading.RLock()
        # default_union makes queries see the union of all named graphs, so a
        # plain `?s ?p ?o` pattern reaches every resource without a GRAPH wrapper.
        self._graph = Dataset(default_union=True)
        storage_root.mkdir(parents=True, exist_ok=True)
        ensure_system_subtree(storage_root)
        with self._lock:
            for path in storage_root.rglob("*.ttl"):
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
            for triple in self._graph.graph(URIRef(uri)):
                result.add(triple)
        return result

    def write(self, uri: str, graph: Graph) -> None:
        assert_public_uri(uri, self._base_uri)
        self.write_system(uri, graph)

    def write_system(self, uri: str, graph: Graph) -> None:
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
        assert_public_uri(uri, self._base_uri)
        self.delete_system(uri)

    def delete_system(self, uri: str) -> None:
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

    def replace_if_unchanged(
        self,
        uri: str,
        graph: Graph,
        expected_etag: str,
        etag_of: Callable[[Graph], str],
    ) -> bool:
        rdf_path = _guard_within_root(
            _uri_to_rdf_path(uri, self._storage_root, self._base_uri), self._storage_root, uri
        )
        subject = URIRef(uri)
        # One lock hold spans the read, the ETag comparison, and the write, so a
        # concurrent delivery bumping the same counter cannot slip in between and be lost.
        with self._lock:
            if not rdf_path.exists():
                return False
            current = Graph()
            for triple in self._graph.graph(subject):
                current.add(triple)
            if etag_of(current) != expected_etag:
                return False
            self.write_system(uri, graph)
            return True

    def stream_binary(self, uri: str, chunk_size: int = 65536) -> Iterator[bytes]:
        bin_path = _guard_within_root(
            _uri_to_bin_path(uri, self._storage_root, self._base_uri), self._storage_root, uri
        )
        if not bin_path.is_file():
            rdf_path = _uri_to_rdf_path(uri, self._storage_root, self._base_uri)
            if rdf_path.exists():
                raise NotABinaryResource(uri)
            raise ResourceNotFound(uri)
        # Stream lazily so large files never load fully into memory
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
            public = [
                graph
                for graph in self._graph.graphs()
                if not str(graph.identifier).startswith(self._system_prefix)
            ]
            scope: Graph = ReadOnlyGraphAggregate(public) if public else Graph()
            return scope.query(sparql, initBindings=bindings)
