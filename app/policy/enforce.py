"""Access-policy enforcement seam for the consumer view pipeline.

The seam sits between parameter binding and CONSTRUCT execution so a policy
decision is made against a fully-validated request before any data is produced.
Today it is a pure pass-through; the enforcement body arrives later.
"""

from app.auth.tokens import TokenRecord
from app.storage.backend import StorageBackend


def check_policy(record: TokenRecord, backend: StorageBackend) -> None:
    """Enforce the access policy referenced by the token record.

    Raises HTTPException(403) when a policy constraint is violated; returns
    normally when the request may proceed. This is currently a pass-through: the
    enforcement body will read the policy RDF at record.policy_ref through backend
    and reject requests that violate it.
    """
    _ = record, backend
