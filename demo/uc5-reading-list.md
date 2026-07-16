# UC5 — A reading list published for a review period

A researcher shares a curated bibliography with a review group — for a fixed
period, with a retrieval budget, and revocably. This scenario exercises the
policy machinery hardest: validity window, per-grant budget, rate limit, a
per-view ceiling shared across all grants, and the owner tightening the terms
while the share is live.

## Setup

A seeded pod (see [the tour](README.md#the-five-minute-tour)):

```sh
# both terminals at the repository root
LDP_ADMIN_TOKEN=devtoken uv run python -m ldp_personal_store.main   # terminal 1
ADMIN=devtoken ./test_data/seed.sh                                  # terminal 2
. test_data/tokens.env
```

The seed created five bibliography entries — four tagged `pods-review-2026`,
each carrying a private rating and note — a `reading-list` view parameterized
by curation tag with a per-view ceiling of 500 deliveries, and
`REVIEW_GROUP_TOKEN` bounded by: valid 2026-07-05 through 2026-08-15, at most
200 retrievals, at least 5 seconds apart.

## Consumer: the curated list

The grants seeded here enforce the 5-second minimum interval, and the seed
itself performed a demo fetch — so if you see `403 min interval not elapsed`,
that is the policy working; wait five seconds and retry.

```sh
curl -G http://localhost:8000/.engine/views/reading-list \
  -H "Authorization: Bearer $REVIEW_GROUP_TOKEN" \
  --data-urlencode "tag=pods-review-2026"
```

Four entries return — Berners-Lee 2001, Speicher 2015, Sambra 2016,
Verborgh 2016 — with title, authors, year, and venue. Absent: the untagged
fifth entry (*Clean Code*, not curated for this review), and every
`bib:rating` and `bib:privateNote` triple. Curation and privacy are both just
the view's `WHERE` clause doing its job.

## Owner: end the review period early

Policies are plain resources; a `PUT` replaces one, and enforcement follows
immediately. Close the window as of July 10 — before "today", July 16:

```sh
curl -X PUT "http://localhost:8000/.system/tokens/policies/$REVIEW_GROUP_RECORD" \
  -H "Authorization: Bearer $ADMIN" -H "Content-Type: text/turtle" \
  --data '
@prefix pod: <https://lukasberka.github.io/ldp-personal-store/vocab#> .
<> a pod:Policy ;
   pod:validFrom  "2026-07-05T00:00:00Z" ;
   pod:validUntil "2026-07-10T00:00:00Z" .'
```

The group's next request is refused, with the violated constraint named:

```sh
curl -sS -w "\nHTTP %{http_code}\n" -G http://localhost:8000/.engine/views/reading-list \
  -H "Authorization: Bearer $REVIEW_GROUP_TOKEN" \
  --data-urlencode "tag=pods-review-2026"
```

```
{"detail":"policy: window elapsed"}
HTTP 403
```

Reopen it by restoring the original policy — a policy edit is always this
`GET`/edit/`PUT` shape, never a new credential:

```sh
curl -X PUT "http://localhost:8000/.system/tokens/policies/$REVIEW_GROUP_RECORD" \
  -H "Authorization: Bearer $ADMIN" -H "Content-Type: text/turtle" \
  --data-binary @test_data/uc5-reading-list/policy-review-group.ttl
```

## Two ceilings, two scopes

The grant's `pod:maxRetrievals 200` meters *this* consumer. The view's own
`pod:maxViewRetrievals 500` — set in the view definition, not the policy —
meters the view across **all** grants that unlock it: a total-exposure bound
on the data itself. Either one, exhausted, answers `403` with its name. For
the smallest possible budget in action, see the single-use grant in
[uc4-shopping.md](uc4-shopping.md).
