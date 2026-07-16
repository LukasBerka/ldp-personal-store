# UC4 — A shopping list shared with a partner

Frequently changing data, read repeatedly by a trusted party, with zero
per-change ceremony: the owner edits the list, the partner's existing token
keeps working, and there is nothing to re-share or re-configure. The same page
also shows the opposite extreme — a **single-use** grant spending its one
retrieval.

## Setup

A seeded pod (see [the tour](README.md#the-five-minute-tour)):

```sh
# both terminals at the repository root
LDP_ADMIN_TOKEN=devtoken uv run python -m ldp_personal_store.main   # terminal 1
ADMIN=devtoken ./test_data/seed.sh                                  # terminal 2
. test_data/tokens.env
```

The seed created `/shopping/list` with six items, a parameter-free
`shopping-list` view, and two grants on it: `HOUSEHOLD_TOKEN`, long-lived with
no policy — the partner — and `SINGLE_USE_TOKEN`, capped at one retrieval.

## Consumer: the week's list

```sh
curl http://localhost:8000/.engine/views/shopping-list \
  -H "Authorization: Bearer $HOUSEHOLD_TOKEN"
```

Six items come back with name, quantity, category, and done flag — milk,
bread, tomatoes, eggs, espresso beans, dish soap.

## Owner: the week moves on

The owner replaces the list — a plain conditional `PUT` on the resource, here
with the prepared next-week state. The pod refuses blind overwrites, so
replacing an existing resource must assert `If-Match` (the careful form is a
`GET`, an edit, and a `PUT` quoting the received entity tag; `*` means "I know
it exists"):

```sh
curl -X PUT http://localhost:8000/shopping/list \
  -H "Authorization: Bearer $ADMIN" -H "Content-Type: text/turtle" \
  -H "If-Match: *" \
  --data-binary @test_data/uc4-shopping/list-week2.ttl
```

## Consumer: same token, new state

```sh
curl http://localhost:8000/.engine/views/shopping-list \
  -H "Authorization: Bearer $HOUSEHOLD_TOKEN"
```

Milk and tomatoes are now marked done, bread and dish soap are gone, butter
and penne have appeared. The partner did nothing — no new link, no new
credential. A view is a live query over the pod's current state, so a share of
changing data is set up once and holds.

## The single-use grant

The other grant on the very same view carries `pod:maxRetrievals 1`. Spend it:

```sh
curl -sS -o /dev/null -w "first:  HTTP %{http_code}\n" \
  http://localhost:8000/.engine/views/shopping-list \
  -H "Authorization: Bearer $SINGLE_USE_TOKEN"

curl -sS -w "\nsecond: HTTP %{http_code}\n" \
  http://localhost:8000/.engine/views/shopping-list \
  -H "Authorization: Bearer $SINGLE_USE_TOKEN"
```

```
first:  HTTP 200
{"detail":"policy: max retrievals reached"}
second: HTTP 403
```

Two consumers, one view, opposite trust levels — the difference is entirely in
the per-grant policy, not in the data or the view.
