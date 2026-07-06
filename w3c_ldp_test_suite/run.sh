#!/usr/bin/env bash
#
# One-command W3C LDP Test Suite run against the pod.
#
# Reports land in w3c_ldp_test_suite/reports/
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPORTS="$HERE/reports"
mkdir -p "$REPORTS"

# Seed a token both pod and proxy share; override by exporting your own.
export LDP_ADMIN_TOKEN="${LDP_ADMIN_TOKEN:-$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')}"

# EARL provenance (describes the tested subject/assertor in the report; does not
# affect test execution). All six are mandatory whenever --earl is passed.
EARL_SOFTWARE="${EARL_SOFTWARE:-Personal LDP Server}"
EARL_DEVELOPER="${EARL_DEVELOPER:-Personal LDP Server}"
EARL_LANGUAGE="${EARL_LANGUAGE:-Python}"
EARL_HOMEPAGE="${EARL_HOMEPAGE:-http://proxy:9000/}"
EARL_ASSERTOR="${EARL_ASSERTOR:-http://proxy:9000/}"
EARL_SHORTNAME="${EARL_SHORTNAME:-personal-ldp-server}"

COMPOSE=(docker compose -f "$HERE/docker-compose.yml")

cleanup() { "${COMPOSE[@]}" down -v --remove-orphans >/dev/null 2>&1 || true; }
trap cleanup EXIT

echo "==> Building images (first run compiles the suite + downloads deps; be patient)…"
"${COMPOSE[@]}" build pod suite

echo "==> Starting pod + proxy…"
"${COMPOSE[@]}" up -d --wait pod proxy

echo "==> Creating Direct container /tck-direct/…"
"${COMPOSE[@]}" exec -T proxy python - <<'PY'
import os, urllib.request
body = b"""@prefix ldp: <http://www.w3.org/ns/ldp#> .
@prefix dcterms: <http://purl.org/dc/terms/> .
<> a ldp:DirectContainer ;
   dcterms:title "LDP test suite direct container" ;
   ldp:membershipResource <> ;
   ldp:hasMemberRelation ldp:member .
"""
req = urllib.request.Request(
    "http://pod:8000/tck-direct/", data=body, method="PUT",
    headers={"Content-Type": "text/turtle",
             "Authorization": "Bearer " + os.environ["LDP_ADMIN_TOKEN"]},
)
with urllib.request.urlopen(req) as r:
    print("   direct container ->", r.status)
PY

# The EARL filename is fixed by the suite (ldp-testsuite-execution-report-earl.*),
# so each leg gets its own --output subdir to avoid the second run clobbering the
# first. --earl is a boolean flag; its six provenance args are passed alongside.
run_suite() {
  local label="$1"; shift
  echo "==> Running suite: $label"
  "${COMPOSE[@]}" run --rm --user "$(id -u):$(id -g)" -T suite \
    "$@" --output "/out/$label" \
    --earl \
    --software "$EARL_SOFTWARE" --developer "$EARL_DEVELOPER" \
    --language "$EARL_LANGUAGE" --homepage "$EARL_HOMEPAGE" \
    --assertor "$EARL_ASSERTOR" --shortname "$EARL_SHORTNAME" \
    2>&1 | tee "$REPORTS/$label.log" || true
  echo
}

# Basic container tests (pod root) + the LDP Non-RDF Source group, then Direct.
# Indirect is intentionally omitted — the pod implements only Basic and Direct.
run_suite basic  --server "http://proxy:9000/"            --basic --non-rdf
run_suite direct --server "http://proxy:9000/tck-direct/" --direct

echo "==> Done. EARL conformance reports:"
echo "   $REPORTS/basic/report/ldp-testsuite-execution-report-earl.ttl"
echo "   $REPORTS/direct/report/ldp-testsuite-execution-report-earl.ttl"
echo "   (alongside: .jsonld, an HTML report, full TestNG output under */test-output/,"
echo "    and the console logs basic.log / direct.log)"
