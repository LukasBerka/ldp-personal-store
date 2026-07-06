"""RDF namespaces and vocabulary terms for the Personal LDP Pod.

Every term the pod uses is declared here as a typed constant so that call-sites
can import names directly (e.g. ``from ldp_personal_store.vocab import LDP_contains``) rather
than accessing dynamic namespace attributes.
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
LDP_membershipResource: URIRef = LDP.membershipResource
LDP_hasMemberRelation: URIRef = LDP.hasMemberRelation
LDP_isMemberOfRelation: URIRef = LDP.isMemberOfRelation


# ---------------------------------------------------------------------------
# Dublin Core Terms — http://purl.org/dc/terms/
# ---------------------------------------------------------------------------
DCTERMS = Namespace("http://purl.org/dc/terms/")

# Predicate recording a non-RDF resource's media type in its metadata sidecar.
# Item access (not ``DCTERMS.format``) is required because ``format`` collides
# with ``str.format`` on the Namespace. This is the placeholder content-type
# term; the HTTP layer may refine it once its serialization needs are settled.
DC_format: URIRef = DCTERMS["format"]

# Human-readable name and description of a view resource. Attribute access is
# safe here (unlike ``format``, neither collides with a Namespace built-in).
DC_title: URIRef = DCTERMS.title
DC_description: URIRef = DCTERMS.description


# ---------------------------------------------------------------------------
# Personal Pod vocabulary — urn:pod:vocab:
# A URN keeps the token-record vocabulary self-contained; no resolvable URI is
# implied. This identifier is stable across restarts and is never derived from
# base_uri — the vocab terms are not runtime-configurable.
# ---------------------------------------------------------------------------
POD = Namespace("urn:pod:vocab:")

# Token record classes
POD_Token: URIRef = POD.Token  # common supertype
POD_ConsumerToken: URIRef = POD.ConsumerToken  # consumer-facing bearer token
POD_AdminToken: URIRef = POD.AdminToken  # pod-owner administrative token
POD_EngineToken: URIRef = POD.EngineToken  # engine-to-storage request-path credential

# Token record properties
POD_tokenHash: URIRef = POD.tokenHash  # xsd:string SHA-256 hex digest
# One-time plaintext in the issuance response body only; never persisted to any record.
POD_tokenSecret: URIRef = POD.tokenSecret
POD_linkedView: URIRef = POD.linkedView  # .system/views/{id}; one triple per unlocked view
POD_policyRef: URIRef = POD.policyRef  # URIRef to .system/tokens/policies/{id}
POD_enforcementCount: URIRef = POD.enforcementCount  # xsd:integer, bumped per delivery
POD_lastUsedAt: URIRef = POD.lastUsedAt  # xsd:dateTime, updated per delivery

# View definition terms
POD_View: URIRef = POD.View  # rdf:type marker for a view resource
POD_constructTemplate: URIRef = POD.constructTemplate  # xsd:string SPARQL CONSTRUCT template
POD_contentTypeHint: URIRef = POD.contentTypeHint  # xsd:string suggested response media type
POD_parameter: URIRef = POD.parameter  # view -> parameter blank node
POD_paramName: URIRef = POD.paramName  # xsd:string SPARQL variable name (no leading '?')
POD_paramType: URIRef = POD.paramType  # xsd:string: 'str' | 'int' | 'iri' | 'date' | 'dateTime'

# Access-policy terms
POD_Policy: URIRef = POD.Policy  # rdf:type marker on a policy resource
POD_expiresAt: URIRef = POD.expiresAt  # xsd:dateTime; grant invalid once now is past this instant
POD_validFrom: URIRef = POD.validFrom  # xsd:dateTime; inclusive lower bound of validity window
POD_validUntil: URIRef = POD.validUntil  # xsd:dateTime; inclusive upper bound of validity window
POD_maxRetrievals: URIRef = POD.maxRetrievals  # xsd:integer; per-grant delivery ceiling
POD_minInterval: URIRef = POD.minInterval  # xsd:integer seconds; min gap between grant deliveries
POD_maxViewRetrievals: URIRef = POD.maxViewRetrievals  # xsd:integer; per-view delivery ceiling
POD_viewRetrievalCount: URIRef = POD.viewRetrievalCount  # xsd:integer; mutable per-view count

# Access-log terms
POD_AccessLogEntry: URIRef = POD.AccessLogEntry  # rdf:type marker on one recorded delivery event
POD_accessLogView: URIRef = POD.accessLogView  # URIRef to the .system/views/{id} that was served
POD_accessLogToken: URIRef = POD.accessLogToken  # URIRef to the authorizing .system/tokens/{id}
POD_accessLogTimestamp: URIRef = POD.accessLogTimestamp  # xsd:dateTime instant delivery was served


# ---------------------------------------------------------------------------
# .system namespace — derived from base URI at runtime
# ---------------------------------------------------------------------------


def make_system_ns(base_uri: str) -> Namespace:
    """Return the .system/ sub-namespace anchored at the pod base URI.

    Example: base_uri="http://localhost:8000/" -> "http://localhost:8000/.system/"

    Called once during lifespan startup. The result is stored on app.state.system_ns
    and shared by every layer that manages .system/ records (tokens, views, policies).
    """
    return Namespace(base_uri.rstrip("/") + "/.system/")


def make_engine_ns(base_uri: str) -> Namespace:
    """Return the .engine/ sub-namespace anchored at the pod base URI.

    Example: base_uri="http://localhost:8000/" -> "http://localhost:8000/.engine/"

    Called once during lifespan startup. The result is stored on app.state.engine_ns
    and is the base for consumer-facing engine URLs (view results and gated blobs).
    """
    return Namespace(base_uri.rstrip("/") + "/.engine/")
