# UC2 — A photo album for a family member

Photos are large binaries with RDF metadata beside them — and some of that
metadata, like GPS coordinates, is private. This scenario shows a consumer
receiving an album as *data*: titles and capture dates as triples about the
image files themselves, with the images streamable through gated links — while
the raw storage stays closed and the coordinates never leave the pod.

## Setup

A seeded pod (see [the tour](README.md#the-five-minute-tour)):

```sh
# both terminals at the repository root
LDP_ADMIN_TOKEN=devtoken uv run python -m ldp_personal_store.main   # terminal 1
ADMIN=devtoken ./test_data/seed.sh                                  # terminal 2
. test_data/tokens.env
```

The seed uploaded three real PNGs to `/photos/summer-2026/` with a metadata
resource each (title, capture date, camera — and private GPS coordinates), an
`album` view projecting only title and date, and `FAMILY_TOKEN`, a grant with
no policy: a trusted family member.

## Consumer: fetch the album

```sh
curl -G http://localhost:8000/.engine/views/album \
  -H "Authorization: Bearer $FAMILY_TOKEN" \
  --data-urlencode "album=http://localhost:8000/photos/summer-2026/"
```

```turtle
<http://localhost:8000/.engine/blob/album?uri=...beach.png&...>
    pho:takenOn "2026-06-28" ;
    dct:title "Morning at the beach" .

<http://localhost:8000/.engine/blob/album?uri=...city.png&...>
    pho:takenOn "2026-07-01" ;
    dct:title "City lights from the fortress" .

<http://localhost:8000/.engine/blob/album?uri=...sunset.png&...>
    pho:takenOn "2026-06-29" ;
    dct:title "Sunset over the old town" .
```

No `pho:gpsLat`, no `pho:gpsLong`, no `pho:camera` — the view never selected
them. And the subjects are not the photos' storage URIs: the engine rewrote
each into a gated `/.engine/blob/` URL, because the consumer could not follow
a raw reference anyway.

## Consumer: download a photo

Copy one rewritten URL out of the result, verbatim, and fetch it with the same
token:

```sh
curl "http://localhost:8000/.engine/blob/album?uri=..." \
  -H "Authorization: Bearer $FAMILY_TOKEN" --output beach.png

file beach.png
# beach.png: PNG image data, 96 x 64, 8-bit/color RGB, non-interlaced
```

The pod streamed the binary in its native `image/png` — content negotiation
delivering non-RDF resources is the point of this use case. In the
[test console](../testing_client/README.md)'s consumer role, these same URLs
appear as download buttons beneath the fetched result.

## The boundary

The gate is the *only* way in. The photo's real URI answers `401` to the
consumer token, like every other storage-surface request:

```sh
curl -sS -o /dev/null -w "HTTP %{http_code}\n" \
  http://localhost:8000/photos/summer-2026/beach.png \
  -H "Authorization: Bearer $FAMILY_TOKEN"
# HTTP 401
```

The gate also tracks the view: a blob URL whose resource has dropped out of
the view's current result answers `404`, so the consumer reaches exactly what
the view currently shares. Each download is a metered delivery of its own —
it appears in the owner's `/.engine/stats` and access log, and counts against
any retrieval ceiling the grant carries.
