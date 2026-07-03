"""Access-policy enforcement for the consumer view pipeline.

The enforcement seam sits between parameter binding and CONSTRUCT execution so a
policy decision is made against a fully-validated request before any upstream data
is produced. A token carries a 1:1 reference to a policy resource; when that
resource exists, its constraints are read from the graph and each is checked
independently. A request that violates any constraint is denied with
HTTPException(403); an absent policy resource, or an absent individual constraint,
is a clean pass-through.
"""

from datetime import UTC, datetime

from fastapi import HTTPException
from rdflib import URIRef

from app.auth.tokens import TokenRecord
from app.storage.backend import ResourceNotFound, StorageBackend
from app.vocab import (
    POD_expiresAt,
    POD_maxRetrievals,
    POD_minInterval,
    POD_validFrom,
    POD_validUntil,
)

# The lastUsedAt sentinel written at mint time. A token still parked at the epoch
# has never had a successful delivery, so the min-interval gate does not apply yet.
UNIX_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def _parse_dt(value: str) -> datetime:
    """Parse an xsd:dateTime lexical form into a tz-aware UTC datetime.

    rdflib serializes these timestamps with a trailing 'Z', which
    datetime.fromisoformat does not accept, so it is normalized to '+00:00' first.
    A value carrying no offset is assumed to be UTC.
    """
    normalized = f"{value[:-1]}+00:00" if value.endswith("Z") else value
    parsed = datetime.fromisoformat(normalized)
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def check_policy(record: TokenRecord, backend: StorageBackend) -> None:
    """Enforce the access policy referenced by the token record.

    Returns normally when the request may proceed, and raises HTTPException(403)
    naming the violated constraint otherwise. Every check runs before any upstream
    user-data read. A token with no policy reference, or one whose policy resource
    was never written, carries no constraint and passes through.
    """
    if record.policy_ref is None:
        return
    try:
        policy = backend.read(record.policy_ref)
    except ResourceNotFound:
        # The policy URI is a stable placeholder on every token; when no graph was
        # ever written there the grant is unconstrained and the request proceeds.
        return

    now = datetime.now(UTC)
    subject = URIRef(record.policy_ref)

    # Boundary rule for the time checks: an instant exactly on a bound is allowed;
    # only a strictly-past (or, for validFrom, strictly-early) instant denies. The
    # validity window is inclusive of both validFrom and validUntil.
    expires_at = policy.value(subject, POD_expiresAt)
    if expires_at is not None and now > _parse_dt(str(expires_at)):
        raise HTTPException(status_code=403, detail="policy: expired")

    valid_from = policy.value(subject, POD_validFrom)
    if valid_from is not None and now < _parse_dt(str(valid_from)):
        raise HTTPException(status_code=403, detail="policy: not yet valid")

    valid_until = policy.value(subject, POD_validUntil)
    if valid_until is not None and now > _parse_dt(str(valid_until)):
        raise HTTPException(status_code=403, detail="policy: window elapsed")

    # Per-grant ceiling: the counter is bumped once per delivery, so at count N-1 the
    # Nth delivery is still allowed and at count N (the limit) the grant is exhausted.
    max_retrievals = policy.value(subject, POD_maxRetrievals)
    if max_retrievals is not None and record.enforcement_count >= int(str(max_retrievals)):
        raise HTTPException(status_code=403, detail="policy: max retrievals reached")

    min_interval = policy.value(subject, POD_minInterval)
    if min_interval is not None:
        # last_used_at is written by the post-delivery counter bump; the epoch sentinel
        # means "never delivered", so the first delivery is always allowed. An elapsed
        # gap exactly equal to the interval passes; only a shorter gap denies.
        last_used = _parse_dt(record.last_used_at)
        if last_used != UNIX_EPOCH and (now - last_used).total_seconds() < int(str(min_interval)):
            raise HTTPException(status_code=403, detail="policy: min interval not elapsed")
