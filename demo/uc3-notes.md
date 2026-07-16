# UC3 — Lecture notes shared with a classmate

A student keeps notes organized by course and topic and occasionally shares a
course folder with a classmate. This scenario is about **hierarchy**: the data
plane is a tree of LDP containers, and the share is scoped to one branch of it
— while the tree itself stays invisible to the consumer.

## Setup

A seeded pod (see [the tour](README.md#the-five-minute-tour)):

```sh
# both terminals at the repository root
LDP_ADMIN_TOKEN=devtoken uv run python -m ldp_personal_store.main   # terminal 1
ADMIN=devtoken ./test_data/seed.sh                                  # terminal 2
. test_data/tokens.env
```

The seed created five notes across two course containers, a `lecture-notes`
view taking the course container as its `lecture` parameter, and
`CLASSMATE_TOKEN`, whose policy simply expires with the semester
(`pod:expiresAt` 2026-09-30).

## Owner: the hierarchy is plain LDP

Organization is nothing view-specific — containers inside containers, listed
by `ldp:contains`, traversable by any LDP-aware client:

```sh
curl http://localhost:8000/notes/ -H "Authorization: Bearer $ADMIN"
```

```turtle
<http://localhost:8000/notes/> a ldp:BasicContainer ;
    ldp:contains <http://localhost:8000/notes/algorithms/>,
        <http://localhost:8000/notes/linear-algebra/> .
```

## Consumer: one course folder

The classmate names the course they were told about and gets its notes in
full — title, topic, date, text:

```sh
curl -G http://localhost:8000/.engine/views/lecture-notes \
  -H "Authorization: Bearer $CLASSMATE_TOKEN" \
  --data-urlencode "lecture=http://localhost:8000/notes/linear-algebra/"
```

```turtle
<http://localhost:8000/.engine/blob/lecture-notes?uri=...eigenvalues&...>
    dct:title "Eigenvalues and diagonalization" ;
    note:topic "spectral-theory" ;
    dct:created "2026-04-21" ;
    note:text "An eigenvector of A is a nonzero v with Av = lv. ..." .

# ...and the course's two other notes; nothing from /notes/algorithms/.
```

The three linear-algebra notes arrive; the algorithms notes do not — the
`WHERE` clause matches only notes whose `note:course` is the requested
container.

## What the scope means — and what it does not

Two boundaries are worth separating, because they are enforced by different
things.

**The hierarchy is never exposed.** The consumer cannot browse the tree or
touch a note directly — the storage surface answers `401` to their token, on
the container listing and on every resource in it:

```sh
curl -sS -o /dev/null -w "HTTP %{http_code}\n" \
  http://localhost:8000/notes/ -H "Authorization: Bearer $CLASSMATE_TOKEN"
# HTTP 401
```

The only reachable form of a note is the gated URL the view's result carries.

**The parameter chooses the folder — by design, any folder.** Try it:

```sh
curl -G http://localhost:8000/.engine/views/lecture-notes \
  -H "Authorization: Bearer $CLASSMATE_TOKEN" \
  --data-urlencode "lecture=http://localhost:8000/notes/algorithms/"
```

The two algorithms notes come back. This seeded view deliberately shares the
whole notes collection *one course at a time*: a consumer-supplied parameter
is part of the owner's sharing decision, and here the decision was "any of my
courses, on request". An owner who wants to pin the share to a single course
authors the view with the folder inlined in the `CONSTRUCT` template and no
parameter at all — the template, not the parameter, is the boundary of what a
grant can ever reach. The [UC1 walkthrough](uc1-calendar.md) shows exactly
that authoring step, and the [UC5 page](uc5-reading-list.md) shows the
complementary bound, a fixed curation tag baked into the data.
