"""The storage dependency seam.

Defines the :class:`StorageBackend` Protocol that every layer above storage
depends on, together with the custom exception hierarchy that is part of the
backend contract. Callers import both the Protocol and the exceptions from this
single module so the seam stays self-contained.
"""

from collections.abc import Iterator
from typing import Protocol, runtime_checkable

from rdflib import Graph
from rdflib.query import Result


class StorageError(Exception): ...


class ResourceNotFound(StorageError): ...  # noqa: N818  intentional domain name


class NotABinaryResource(StorageError): ...  # noqa: N818  intentional domain name


class PrefixViolation(StorageError): ...  # noqa: N818  intentional domain name


@runtime_checkable
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

    def query(self, sparql: str, init_bindings: dict[str, str] | None = None) -> Result:
        """Execute *sparql* against the union of all resource graphs.

        Returns the raw rdflib Result (SELECT rows, CONSTRUCT graph, or ASK boolean);
        serialization is the caller's responsibility.
        """
        ...
