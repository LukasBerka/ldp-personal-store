#!/usr/bin/env bash
# Seed the pod with concrete test data for the thesis use cases UC1-UC7
# (bachelor-thesis/text/ch3_requirements.tex, section 3.2).
#
# Usage:
#   LDP_ADMIN_TOKEN=devtoken uv run python -m ldp_personal_store.main   # in one terminal
#   ADMIN=devtoken ./test_data/seed.sh                   # in another
#
# Environment:
#   ADMIN  (required) pod admin token (LDP_ADMIN_TOKEN or the one logged at boot)
#   BASE   (optional) pod base URL, default http://localhost:8000
#
# The script is idempotent for resources and views (PUT). Each run mints a
# fresh set of consumer tokens and writes them to test_data/tokens.env.
set -euo pipefail

BASE="${BASE:-http://localhost:8000}"
BASE="${BASE%/}"
ADMIN="${ADMIN:?Set ADMIN to the pod admin token}"
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEFAULT_BASE="http://localhost:8000"
TOKENS_ENV="$DIR/tokens.env"

auth=(-H "Authorization: Bearer $ADMIN")
turtle=(-H "Content-Type: text/turtle")

# Substitute the default base URI baked into the .ttl files when the pod runs
# elsewhere, and stream the result to stdout.
body() { sed "s|$DEFAULT_BASE|$BASE|g" "$1"; }

# ldp_put <path> <content-type> <curl-data-arg>... : idempotent PUT of an LDP
# resource. The LDP router refuses a blind overwrite of an existing resource
# with 428, so create-only first and fall back to an unconditional overwrite
# when the resource is already there. The data args must be re-readable across
# both attempts (a literal --data string or an @file), never a consumed pipe.
ldp_put() {
  local path="$1" ct="$2"; shift 2
  local url="$BASE/$path" code
  code="$(curl -sS -o /dev/null -w '%{http_code}' -X PUT "$url" \
    "${auth[@]}" -H "Content-Type: $ct" -H "If-None-Match: *" "$@")"
  if [ "$code" = 412 ]; then
    code="$(curl -sS -o /dev/null -w '%{http_code}' -X PUT "$url" \
      "${auth[@]}" -H "Content-Type: $ct" -H "If-Match: *" "$@")"
  fi
  case "$code" in
    2??) ;;
    *) echo "ERROR: PUT /$path returned HTTP $code" >&2; exit 1 ;;
  esac
}

put_container() { # <path ending in />
  ldp_put "$1" "text/turtle" --data "<$BASE/$1> a <http://www.w3.org/ns/ldp#BasicContainer> ."
}

put_rdf() { # <path> <file>
  local tmp; tmp="$(mktemp)"
  body "$2" > "$tmp"
  ldp_put "$1" "text/turtle" --data-binary "@$tmp"
  rm -f "$tmp"
}

put_binary() { # <path> <file> <content-type>
  ldp_put "$1" "$3" --data-binary "@$2"
}

put_view() { # <view-id> <file>
  body "$2" | curl -fsS -o /dev/null -X PUT "$BASE/.system/views/$1" "${auth[@]}" "${turtle[@]}" --data-binary @-
}

# mint_token <var-prefix> <view-id>... : POST a grant unlocking the given
# views; stores the plaintext secret in ${prefix}_TOKEN and the record id in
# ${prefix}_RECORD (the record id is also the policy id).
mint_token() {
  local prefix="$1"; shift
  local triples="" v
  for v in "$@"; do
    triples+="<> <https://lukasberka.github.io/ldp-personal-store/vocab#linkedView> <$BASE/.system/views/$v> . "
  done
  local hdr; hdr="$(mktemp)"
  local resp
  resp="$(curl -fsS -D "$hdr" -X POST "$BASE/.system/tokens" "${auth[@]}" "${turtle[@]}" --data "$triples")"
  local secret record
  secret="$(printf '%s' "$resp" | LC_ALL=C sed -n 's/.*tokenSecret[^"]*"\([^"]*\)".*/\1/p' | head -n1)"
  record="$(grep -i '^location:' "$hdr" | tr -d '\r' | awk '{print $2}')"
  record="${record##*/}"
  rm -f "$hdr"
  [ -n "$secret" ] || { echo "ERROR: could not extract tokenSecret for $prefix" >&2; exit 1; }
  printf -v "${prefix}_TOKEN" '%s' "$secret"
  printf -v "${prefix}_RECORD" '%s' "$record"
}

put_policy() { # <record-id> <file>
  body "$2" | curl -fsS -o /dev/null -X PUT "$BASE/.system/tokens/policies/$1" "${auth[@]}" "${turtle[@]}" --data-binary @-
}

fetch_view() { # <token> <view-id> [--data-urlencode k=v]...
  local token="$1" view="$2"; shift 2
  curl -fsS -G -o /dev/null "$BASE/.engine/views/$view" -H "Authorization: Bearer $token" "$@"
}

# UC1: Calendar
put_container "calendar/"
put_container "calendar/work/"
put_container "calendar/personal/"
for f in "$DIR"/uc1-calendar/work/*.ttl; do
  put_rdf "calendar/work/$(basename "${f%.ttl}")" "$f"
done
for f in "$DIR"/uc1-calendar/personal/*.ttl; do
  put_rdf "calendar/personal/$(basename "${f%.ttl}")" "$f"
done
put_view "schedule" "$DIR/uc1-calendar/view-schedule.ttl"
put_view "schedule-window" "$DIR/uc1-calendar/view-schedule-window.ttl"

# UC2: Photos
put_container "photos/"
put_container "photos/summer-2026/"
for name in beach sunset city; do
  put_binary "photos/summer-2026/$name.png" "$DIR/uc2-photos/$name.png" "image/png"
  put_rdf "photos/summer-2026/$name-meta" "$DIR/uc2-photos/$name-meta.ttl"
done
put_view "album" "$DIR/uc2-photos/view-album.ttl"

# UC3: Lecture notes
put_container "notes/"
put_container "notes/linear-algebra/"
put_container "notes/algorithms/"
for f in "$DIR"/uc3-notes/linear-algebra/*.ttl; do
  put_rdf "notes/linear-algebra/$(basename "${f%.ttl}")" "$f"
done
for f in "$DIR"/uc3-notes/algorithms/*.ttl; do
  put_rdf "notes/algorithms/$(basename "${f%.ttl}")" "$f"
done
put_view "lecture-notes" "$DIR/uc3-notes/view-lecture-notes.ttl"

# UC4: Shopping list
put_container "shopping/"
put_rdf "shopping/list" "$DIR/uc4-shopping/list.ttl"
put_view "shopping-list" "$DIR/uc4-shopping/view-shopping-list.ttl"

# UC5: Reading list
put_container "reading-list/"
for f in "$DIR"/uc5-reading-list/entries/*.ttl; do
  put_rdf "reading-list/$(basename "${f%.ttl}")" "$f"
done
put_view "reading-list" "$DIR/uc5-reading-list/view-reading-list.ttl"

# One credential per consumer; the colleague's unlocks two views (FR4).
mint_token COLLEAGUE schedule schedule-window
mint_token FAMILY album
mint_token CLASSMATE lecture-notes
mint_token HOUSEHOLD shopping-list
mint_token REVIEW_GROUP reading-list
# Single-use demo grant on the param-free shopping-list view (maxRetrievals 1).
mint_token SINGLE_USE shopping-list

# Demo fetches populate the UC7 access log; run before the policies so the 5s
# minInterval does not throttle seeding.
fetch_view "$COLLEAGUE_TOKEN" schedule --data-urlencode "calendar=$BASE/calendar/work/"
fetch_view "$COLLEAGUE_TOKEN" schedule-window \
  --data-urlencode "calendar=$BASE/calendar/work/" \
  --data-urlencode "from=2026-07-05" --data-urlencode "to=2026-07-19"
fetch_view "$FAMILY_TOKEN" album --data-urlencode "album=$BASE/photos/summer-2026/"
fetch_view "$CLASSMATE_TOKEN" lecture-notes --data-urlencode "lecture=$BASE/notes/linear-algebra/"
fetch_view "$HOUSEHOLD_TOKEN" shopping-list
fetch_view "$HOUSEHOLD_TOKEN" shopping-list   # second household member
fetch_view "$REVIEW_GROUP_TOKEN" reading-list --data-urlencode "tag=pods-review-2026"
# The single-use grant is deliberately not fetched here so its one retrieval is
# left for you to spend when testing the ceiling.

# Policies (family and household grants stay unconstrained by design).
put_policy "$COLLEAGUE_RECORD" "$DIR/uc1-calendar/policy-colleague.ttl"
put_policy "$CLASSMATE_RECORD" "$DIR/uc3-notes/policy-classmate.ttl"
put_policy "$REVIEW_GROUP_RECORD" "$DIR/uc5-reading-list/policy-review-group.ttl"
put_policy "$SINGLE_USE_RECORD" "$DIR/policy-single-use.ttl"

{
  echo "# Generated by seed.sh on $(date -u +%Y-%m-%dT%H:%M:%SZ) against $BASE"
  echo "# source this file: . test_data/tokens.env"
  for p in COLLEAGUE FAMILY CLASSMATE HOUSEHOLD REVIEW_GROUP SINGLE_USE; do
    tok="${p}_TOKEN"; rec="${p}_RECORD"
    echo "export ${p}_TOKEN='${!tok}'"
    echo "export ${p}_RECORD='${!rec}'"
  done
} > "$TOKENS_ENV"
chmod 600 "$TOKENS_ENV"

echo "UC1: Calendar (ID: schedule, schedule-window) - generated. Consumer token: $COLLEAGUE_TOKEN"
echo "UC2: Photos (ID: album) - generated. Consumer token: $FAMILY_TOKEN"
echo "UC3: Lecture notes (ID: lecture-notes) - generated. Consumer token: $CLASSMATE_TOKEN"
echo "UC4: Shopping list (ID: shopping-list) - generated. Consumer token: $HOUSEHOLD_TOKEN"
echo "UC5: Reading list (ID: reading-list) - generated. Consumer token: $REVIEW_GROUP_TOKEN"
echo "Single-use (maxRetrievals=1, ID: shopping-list) - generated. Consumer token: $SINGLE_USE_TOKEN"
