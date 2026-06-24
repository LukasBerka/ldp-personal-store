"""RDF namespaces and vocabulary terms for the Personal LDP Pod.

All terms used across phases are declared here as typed constants so that
call-sites can import names directly (e.g. ``from app.vocab import LDP_contains``)
rather than accessing dynamic namespace attributes.
"""

from rdflib.namespace import Namespace
from rdflib.term import URIRef

# ---------------------------------------------------------------------------
# Linked Data Platform — http://www.w3.org/ns/ldp#
# Not built into rdflib; must be declared manually.
# ---------------------------------------------------------------------------
LDP = Namespace("http://www.w3.org/ns/ldp#")

# LDP classes
LDP_Resource: URIRef = LDP.Resource
LDP_RDFSource: URIRef = LDP.RDFSource
LDP_NonRDFSource: URIRef = LDP.NonRDFSource
LDP_Container: URIRef = LDP.Container
LDP_BasicContainer: URIRef = LDP.BasicContainer
LDP_DirectContainer: URIRef = LDP.DirectContainer

# LDP properties
LDP_contains: URIRef = LDP.contains
LDP_member: URIRef = LDP.member
LDP_membershipResource: URIRef = LDP.membershipResource
LDP_hasMemberRelation: URIRef = LDP.hasMemberRelation
LDP_isMemberOfRelation: URIRef = LDP.isMemberOfRelation
LDP_insertedContentRelation: URIRef = LDP.insertedContentRelation
LDP_constrainedBy: URIRef = LDP.constrainedBy

# LDP preference URIs
LDP_PreferContainment: URIRef = LDP.PreferContainment
LDP_PreferMembership: URIRef = LDP.PreferMembership
LDP_PreferMinimalContainer: URIRef = LDP.PreferMinimalContainer


# ---------------------------------------------------------------------------
# Dublin Core Terms — http://purl.org/dc/terms/
# ---------------------------------------------------------------------------
DCTERMS = Namespace("http://purl.org/dc/terms/")

# Predicate recording a non-RDF resource's media type in its metadata sidecar.
# Item access (not ``DCTERMS.format``) is required because ``format`` collides
# with ``str.format`` on the Namespace. This is the placeholder content-type
# term; the HTTP layer may refine it once its serialization needs are settled.
DC_format: URIRef = DCTERMS["format"]


# ---------------------------------------------------------------------------
# .system namespace — derived from base URI at runtime
# ---------------------------------------------------------------------------


def make_system_ns(base_uri: str) -> Namespace:
    """Return the .system/ sub-namespace anchored at the pod base URI.

    Example: base_uri="http://localhost:8000/" -> "http://localhost:8000/.system/"

    Called once during lifespan startup. The result is stored on app.state.system_ns
    and shared with later phases (tokens, views, policies all live under .system/).
    """
    return Namespace(base_uri.rstrip("/") + "/.system/")
