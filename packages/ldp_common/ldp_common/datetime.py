"""xsd:dateTime parsing shared by policy enforcement and access-log validation."""

from datetime import UTC, datetime

# The lastUsedAt sentinel written at issue time. A token still parked at the epoch
# has never had a successful delivery, so the min-interval gate does not apply yet.
UNIX_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def parse_xsd_datetime(value: str) -> datetime:
    """Parse an xsd:dateTime lexical form into a tz-aware UTC datetime."""
    normalized = f"{value[:-1]}+00:00" if value.endswith("Z") else value
    parsed = datetime.fromisoformat(normalized)
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
