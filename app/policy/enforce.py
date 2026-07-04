"""Access-policy enforcement for the consumer view pipeline.

The enforcement seam sits between parameter binding and CONSTRUCT execution so a
policy decision is made against a fully-validated request before any upstream data
is produced. A token carries a 1:1 reference to a policy resource; the engine
fetches that resource (and already holds the view record) over the storage
boundary and hands both graphs to :func:`check_policy`, which is a pure decision:
each constraint present is checked independently, a request that violates any
constraint is denied with HTTPException(403), and an absent policy graph, or an
absent individual constraint, is a clean pass-through.
"""

from datetime import UTC, datetime

from fastapi import HTTPException
from rdflib import Graph, URIRef

from app.auth.tokens import TokenRecord
from app.vocab import (
    POD_expiresAt,
    POD_maxRetrievals,
    POD_maxViewRetrievals,
    POD_minInterval,
    POD_validFrom,
    POD_validUntil,
    POD_viewRetrievalCount,
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


def check_policy(
    record: TokenRecord,
    policy_graph: Graph | None,
    view_graph: Graph | None,
) -> None:
    """Enforce the access policy referenced by the token record.

    Returns normally when the request may proceed, and raises HTTPException(403)
    naming the violated constraint otherwise. *policy_graph* is None when the
    token carries no policy reference or the policy resource was never written —
    such a grant carries no constraint and passes through. *view_graph* carries
    the per-view ceiling and its counter when the token is scoped to a view.
    """
    if policy_graph is None:
        return

    now = datetime.now(UTC)
    subject = URIRef(record.policy_ref or "")

    # Boundary rule for the time checks: an instant exactly on a bound is allowed;
    # only a strictly-past (or, for validFrom, strictly-early) instant denies. The
    # validity window is inclusive of both validFrom and validUntil.
    expires_at = policy_graph.value(subject, POD_expiresAt)
    if expires_at is not None and now > _parse_dt(str(expires_at)):
        raise HTTPException(status_code=403, detail="policy: expired")

    valid_from = policy_graph.value(subject, POD_validFrom)
    if valid_from is not None and now < _parse_dt(str(valid_from)):
        raise HTTPException(status_code=403, detail="policy: not yet valid")

    valid_until = policy_graph.value(subject, POD_validUntil)
    if valid_until is not None and now > _parse_dt(str(valid_until)):
        raise HTTPException(status_code=403, detail="policy: window elapsed")

    # Per-grant ceiling: the counter is bumped once per delivery, so at count N-1 the
    # Nth delivery is still allowed and at count N (the limit) the grant is exhausted.
    max_retrievals = policy_graph.value(subject, POD_maxRetrievals)
    if max_retrievals is not None and record.enforcement_count >= int(str(max_retrievals)):
        raise HTTPException(status_code=403, detail="policy: max retrievals reached")

    min_interval = policy_graph.value(subject, POD_minInterval)
    if min_interval is not None:
        # last_used_at is written by the post-delivery counter bump; the epoch sentinel
        # means "never delivered", so the first delivery is always allowed. An elapsed
        # gap exactly equal to the interval passes; only a shorter gap denies.
        last_used = _parse_dt(record.last_used_at)
        if last_used != UNIX_EPOCH and (now - last_used).total_seconds() < int(str(min_interval)):
            raise HTTPException(status_code=403, detail="policy: min interval not elapsed")

    # Per-view ceiling: a limit shared across every grant on the same view, held on
    # the view record the engine already fetched for this request. Same
    # documented-acceptable TOCTOU window as the per-grant counter — the count read
    # here can race the post-delivery bump under concurrent requests, which is
    # tolerated for a single-user pod.
    if view_graph is not None and record.linked_view_uri is not None:
        view_subject = URIRef(record.linked_view_uri)
        view_limit = view_graph.value(view_subject, POD_maxViewRetrievals)
        if view_limit is not None:
            current = view_graph.value(view_subject, POD_viewRetrievalCount)
            if int(str(current or 0)) >= int(str(view_limit)):
                raise HTTPException(status_code=403, detail="policy: view retrievals reached")
