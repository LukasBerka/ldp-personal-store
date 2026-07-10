"""The storage dependency seam."""

from collections.abc import Callable, Iterator
from typing import Protocol

from rdflib import Graph
from rdflib.query import Result


class StorageError(Exception): ...


class ResourceNotFound(StorageError): ...  # noqa: N818  intentional domain name


class NotABinaryResource(StorageError): ...  # noqa: N818  intentional domain name


class PrefixViolation(StorageError): ...  # noqa: N818  intentional domain name


class StorageBackend(Protocol):
    """Read, write, delete, and query RDF and binary resources by URI.

    Implementations are fully synchronous: rdflib and filesystem I/O are blocking,
    and FastAPI runs synchronous path operations in a threadpool.
    """

    def read(self, uri: str) -> Graph:
        """Return a flat Graph holding only the triples for the resource at *uri*.

        Raises ResourceNotFound if no resource exists at *uri*.
        """
        ...

    def write(self, uri: str, graph: Graph) -> None:
        """Persist *graph* as the RDF resource at *uri*, creating or replacing it.

        Raises PrefixViolation if *uri* is under the reserved .system/ subtree.
        """
        ...

    def write_binary(self, uri: str, data: bytes, content_type: str) -> None:
        """Persist raw *data* as the binary resource at *uri*, creating or replacing it.

        Raises PrefixViolation if *uri* is under the reserved .system/ subtree.
        """
        ...

    def delete(self, uri: str) -> None:
        """Remove the RDF or binary resource at *uri*.

        Raises ResourceNotFound if no resource exists at *uri*.
        """
        ...

    def stream_binary(self, uri: str, chunk_size: int = 65536) -> Iterator[bytes]:
        """Yield the bytes of the binary resource at *uri* in *chunk_size* chunks.

        Raises ResourceNotFound if no resource exists at *uri*.
        Raises NotABinaryResource if *uri* identifies an RDF resource.
        """
        ...

    def query(
        self,
        sparql: str,
        init_bindings: dict[str, str] | None = None,
        include_system: bool = False,
        init_binding_types: dict[str, str] | None = None,
    ) -> Result:
        """Execute *sparql* against the union of all resource graphs.

        By default the reserved ``.system/`` graphs (views, tokens, policies, the
        access log) are excluded from evaluation, so a query run on behalf of a
        view can never read server-managed records. Internal callers that operate
        on those records pass ``include_system=True``; only that full-dataset scope
        exposes the named-graph axis, so ``GRAPH`` clauses require it.

        ``init_binding_types`` optionally maps a binding name to an XSD datatype IRI
        so that value binds as a typed RDF term (``"2026-07-06"^^xsd:date``) instead
        of a plain literal; names absent from the map keep the default term coercion.

        Returns the raw rdflib Result (SELECT rows, CONSTRUCT graph, or ASK boolean);
        serialization is the caller's responsibility.
        """
        ...

    def write_system(self, uri: str, graph: Graph) -> None:
        """Persist *graph* as the resource at *uri* WITHOUT the public prefix guard.

        The server-managed counterpart to write(): it accepts .system/ URIs so the
        token layer can store its records. Never expose this to owner-supplied input;
        public writes must go through write(), which rejects the .system/ subtree.
        """
        ...

    def delete_system(self, uri: str) -> None:
        """Remove the server-managed resource at *uri* under .system/.

        The internal revocation path: unlike delete(), it applies no prefix guard,
        so it can remove .system/ records the public API must never touch.

        Raises ResourceNotFound if no resource exists at *uri*.
        """
        ...

    def replace_if_unchanged(
        self,
        uri: str,
        graph: Graph,
        expected_etag: str,
        etag_of: Callable[[Graph], str],
    ) -> bool:
        """Atomically replace the .system/ record at *uri* with *graph* iff unchanged.

        The read-compare-write runs under a single lock acquisition, so no concurrent
        write can interleave between the ETag check and the replacement — the primitive
        that makes the engine's conditional ``PUT`` free of lost updates. Returns False
        without writing when the record is absent or its current representation no longer
        hashes to *expected_etag*; the caller surfaces that as a 412 so the client
        re-reads and retries. *etag_of* is the HTTP layer's ETag function, injected so
        this storage interface carries no dependency on it.
        """
        ...
