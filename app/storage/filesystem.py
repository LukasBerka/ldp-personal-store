"""Filesystem-backed :class:`StorageBackend` over an in-memory rdflib graph.

RDF resources are Turtle files; binary resources are raw bytes plus a Turtle
metadata sidecar. On construction the backend walks ``storage_root`` and rebuilds
a :class:`~rdflib.ConjunctiveGraph` holding one named graph per resource URI, so a
fresh instance reconstructs the same state from disk. Disk stays authoritative:
each write/delete updates the named graph incrementally rather than rebuilding.
"""

import threading
from collections.abc import Iterator
from pathlib import Path

from rdflib import ConjunctiveGraph, Graph, URIRef
from rdflib.query import Result

from app.storage.backend import StorageError
from app.storage.system import ensure_system_subtree


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
        # ConjunctiveGraph is not thread-safe; this guards every read-modify-write
        # so concurrent threadpool requests cannot corrupt the in-memory store.
        self._lock = threading.RLock()
        self._graph = ConjunctiveGraph()
        storage_root.mkdir(parents=True, exist_ok=True)
        ensure_system_subtree(storage_root)
        with self._lock:
            for path in storage_root.rglob("*.ttl"):
                if path.name.endswith(".meta.ttl"):
                    continue
                uri = _path_to_uri(path, storage_root, base_uri)
                context = self._graph.get_context(URIRef(uri))
                context.parse(data=path.read_text(encoding="utf-8"), format="turtle")

    def read(self, uri: str) -> Graph:
        raise NotImplementedError

    def write(self, uri: str, graph: Graph) -> None:
        raise NotImplementedError

    def write_binary(self, uri: str, data: bytes, content_type: str) -> None:
        raise NotImplementedError

    def delete(self, uri: str) -> None:
        raise NotImplementedError

    def stream_binary(self, uri: str, chunk_size: int = 65536) -> Iterator[bytes]:
        raise NotImplementedError

    def query(self, sparql: str, init_bindings: dict[str, str] | None = None) -> Result:
        raise NotImplementedError
