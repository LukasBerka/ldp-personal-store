"""The pod's reserved top-level names and the prefix invariant that guards them.

The pod owner owns every top-level name under the base URI except the ones the
pod claims for itself: the ``.system/`` sub-tree (server-managed views, tokens,
policies, and the access log), the ``.engine/`` sub-tree (the view engine's
consumer-facing namespace — pure routing, never stored), and the fixed endpoint
names ``sparql`` and ``health``. Public write operations must never touch any of
them — a stored resource there would be shadowed by the pod's own routes — so
the backend calls :func:`assert_public_uri` before persisting owner-supplied
resources. Server-managed ``.system/`` writes go through the separate
``write_system`` path instead.
"""

from pathlib import Path

from ldp_personal_store.storage.backend import PrefixViolation

SYSTEM_SEGMENT = ".system"

RESERVED_SEGMENTS: frozenset[str] = frozenset({SYSTEM_SEGMENT, ".engine", "sparql", "health"})


def ensure_system_subtree(storage_root: Path) -> None:
    """Create the reserved ``.system/`` directory tree under *storage_root*.

    Idempotent: existing directories are left untouched.
    """
    for subdir in ("views", "tokens", "tokens/policies", "access-log"):
        (storage_root / SYSTEM_SEGMENT / subdir).mkdir(parents=True, exist_ok=True)


def assert_public_uri(uri: str, base_uri: str) -> None:
    """Reject *uri* when its first path segment is one of the pod's reserved names.

    Raises PrefixViolation for any URI the owner is not allowed to write directly.
    """
    segment = uri.removeprefix(base_uri).split("/")[0]
    if segment in RESERVED_SEGMENTS:
        raise PrefixViolation(f"URI {uri!r} is under the reserved name {segment!r}")
