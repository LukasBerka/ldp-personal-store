"""Access-policy enforcement for the consumer view pipeline."""

from datetime import UTC, datetime

from fastapi import HTTPException
from rdflib import Graph, URIRef

from ldp_common.datetime import UNIX_EPOCH, parse_xsd_datetime
from ldp_common.tokenrecord import TokenRecord
from ldp_common.vocabulary import (
    POD_expiresAt,
    POD_maxRetrievals,
    POD_maxViewRetrievals,
    POD_minInterval,
    POD_validFrom,
    POD_validUntil,
    POD_viewRetrievalCount,
)


def check_policy(
    record: TokenRecord,
    policy_graph: Graph | None,
    view_graph: Graph | None,
    view_uri: str | None,
) -> None:
    """Enforce the access policy referenced by the token record."""
    # Per-grant constraints come from the grant's own policy graph.
    if policy_graph is not None:
        now = datetime.now(UTC)
        subject = URIRef(record.policy_ref or "")

        # validity window is inclusive of both validFrom and validUntil.
        expires_at = policy_graph.value(subject, POD_expiresAt)
        if expires_at is not None and now > parse_xsd_datetime(str(expires_at)):
            raise HTTPException(status_code=403, detail="policy: expired")

        valid_from = policy_graph.value(subject, POD_validFrom)
        if valid_from is not None and now < parse_xsd_datetime(str(valid_from)):
            raise HTTPException(status_code=403, detail="policy: not yet valid")

        valid_until = policy_graph.value(subject, POD_validUntil)
        if valid_until is not None and now > parse_xsd_datetime(str(valid_until)):
            raise HTTPException(status_code=403, detail="policy: window elapsed")

        max_retrievals = policy_graph.value(subject, POD_maxRetrievals)
        if max_retrievals is not None and record.enforcement_count >= int(str(max_retrievals)):
            raise HTTPException(status_code=403, detail="policy: max retrievals reached")

        min_interval = policy_graph.value(subject, POD_minInterval)
        if min_interval is not None:
            last_used = parse_xsd_datetime(record.last_used_at)
            if last_used != UNIX_EPOCH and (now - last_used).total_seconds() < int(
                str(min_interval)
            ):
                raise HTTPException(status_code=403, detail="policy: min interval not elapsed")

    if view_graph is not None and view_uri is not None:
        view_subject = URIRef(view_uri)
        view_limit = view_graph.value(view_subject, POD_maxViewRetrievals)
        if view_limit is not None:
            current = view_graph.value(view_subject, POD_viewRetrievalCount)
            if int(str(current or 0)) >= int(str(view_limit)):
                raise HTTPException(status_code=403, detail="policy: view retrievals reached")
