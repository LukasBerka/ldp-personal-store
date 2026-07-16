# UC1 — Share a calendar with a colleague, built by hand

The opening use case: the owner keeps personal events in the pod and
wants a colleague to see the schedule for the next two weeks — **without**
exposing the private notes, attendee lists, or locations stored with each
event. This page builds that share from nothing, one request at a time, so you
perform every step of the owner → consumer flow yourself:

1. create a calendar and insert events (the data plane),
2. author a **view** — a SPARQL `CONSTRUCT` that projects only titles and dates,
3. issue a **grant** — the bearer token that unlocks the view,
4. bound it with a **policy** — expiry, retrieval budget, rate limit,
5. consume it — discovery, parameterized fetch, and the denials that prove the
   boundaries hold,
6. audit and revoke.

Every command was run against a pod as written; the responses shown are real.

## Setup

Both terminals must sit at the **repository root**: the pod runs from there,
and the `@test_data/...` request bodies below are read by curl relative to it.

Start a **fresh** pod (the walkthrough creates resources, and the pod refuses
blind overwrites of existing ones with `428` — to reset, stop the pod and
delete its storage directory, `./data` by default):

```sh
# terminal 1 — at the repository root
LDP_ADMIN_TOKEN=devtoken uv run python -m ldp_personal_store.main
```

In the second terminal, bind the admin token once so the commands below paste
as written:

```sh
# terminal 2 — at the repository root
export ADMIN=devtoken
```

## 1. Owner: create the calendar

Containers are created with `PUT`; the body types the resource as one:

```sh
curl -X PUT http://localhost:8000/calendar/ \
  -H "Authorization: Bearer $ADMIN" -H "Content-Type: text/turtle" \
  --data '<> a <http://www.w3.org/ns/ldp#BasicContainer> .'

curl -X PUT http://localhost:8000/calendar/work/ \
  -H "Authorization: Bearer $ADMIN" -H "Content-Type: text/turtle" \
  --data '<> a <http://www.w3.org/ns/ldp#BasicContainer> .'
```

Both answer `201 Created`.

## 2. Owner: insert events

The first event, typed in full so you can see what the pod actually stores —
note the `cal:privateNote` and `cal:attendee` triples that must never reach the
colleague:

```sh
curl -X PUT http://localhost:8000/calendar/work/standup \
  -H "Authorization: Bearer $ADMIN" -H "Content-Type: text/turtle" \
  --data '
@prefix dct: <http://purl.org/dc/terms/> .
@prefix cal: <http://example.org/cal#> .
<> a cal:Event ;
   dct:title "Team standup" ;
   cal:date "2026-07-06" ;
   cal:calendar <http://localhost:8000/calendar/work/> ;
   cal:privateNote "Discuss the project goals." ;
   cal:attendee "alice@example.org", "bob@example.org" .'
```

Two more events come from the prepared files in `test_data/` — one inside the
coming fortnight, one deliberately outside it (August 3), so the date window
has something to exclude:

```sh
curl -X PUT http://localhost:8000/calendar/work/sprint-review \
  -H "Authorization: Bearer $ADMIN" -H "Content-Type: text/turtle" \
  --data-binary @test_data/uc1-calendar/work/sprint-review.ttl

curl -X PUT http://localhost:8000/calendar/work/quarterly-planning \
  -H "Authorization: Bearer $ADMIN" -H "Content-Type: text/turtle" \
  --data-binary @test_data/uc1-calendar/work/quarterly-planning.ttl
```

The pod maintains containment; reading the container back lists all three:

```sh
curl http://localhost:8000/calendar/work/ -H "Authorization: Bearer $ADMIN"
```

```turtle
<http://localhost:8000/calendar/work/> a ldp:BasicContainer ;
    ldp:contains <http://localhost:8000/calendar/work/quarterly-planning>,
        <http://localhost:8000/calendar/work/sprint-review>,
        <http://localhost:8000/calendar/work/standup> .
```

## 3. Owner: author the view

A view is a named SPARQL `CONSTRUCT` template with declared, typed parameters.
This is the entire sharing decision, in one file
(`test_data/uc1-calendar/view-schedule-window.ttl`):

```turtle
<> a pod:View ;
   dcterms:title "Schedule for a date window" ;
   pod:constructTemplate """
     PREFIX dct: <http://purl.org/dc/terms/>
     PREFIX cal: <http://example.org/cal#>
     CONSTRUCT { ?e dct:title ?t ; cal:date ?d }
     WHERE     { ?e dct:title ?t ; cal:date ?d ;
                    cal:calendar ?calendar .
                 FILTER(STR(?d) >= STR(?from) && STR(?d) <= STR(?to)) }
   """ ;
   pod:contentTypeHint "text/turtle" ;
   pod:parameter [ pod:paramName "calendar" ; pod:paramType "iri" ] ,
                 [ pod:paramName "from"     ; pod:paramType "str" ] ,
                 [ pod:paramName "to"       ; pod:paramType "str" ] .
```

The `CONSTRUCT` head names what leaves the pod — titles and dates, nothing
else. Install it, and its unwindowed sibling, under the ids consumers will use:

```sh
curl -X PUT http://localhost:8000/.system/views/schedule-window \
  -H "Authorization: Bearer $ADMIN" -H "Content-Type: text/turtle" \
  --data-binary @test_data/uc1-calendar/view-schedule-window.ttl

curl -X PUT http://localhost:8000/.system/views/schedule \
  -H "Authorization: Bearer $ADMIN" -H "Content-Type: text/turtle" \
  --data-binary @test_data/uc1-calendar/view-schedule.ttl
```

A template that is not valid `CONSTRUCT`, or declares a parameter it never
uses, is rejected at authoring time with `422`.

## 4. Owner: issue the grant

One credential can unlock several views — here both. The `-D -` prints
the response headers, because you need two things from this response:

```sh
curl -sS -D - -X POST http://localhost:8000/.system/tokens \
  -H "Authorization: Bearer $ADMIN" -H "Content-Type: text/turtle" \
  --data '
@prefix pod: <https://lukasberka.github.io/ldp-personal-store/vocab#> .
<> <http://purl.org/dc/terms/title> "colleague-schedule" ;
   pod:linkedView <http://localhost:8000/.system/views/schedule> ;
   pod:linkedView <http://localhost:8000/.system/views/schedule-window> .'
```

```
HTTP/1.1 201 Created
location: http://localhost:8000/.system/tokens/WnfAUkmfeHo
...
<http://localhost:8000/.system/tokens/WnfAUkmfeHo> a pod:ConsumerToken ;
    pod:linkedView <http://localhost:8000/.system/views/schedule>, ... ;
    pod:policyRef <http://localhost:8000/.system/tokens/policies/WnfAUkmfeHo> ;
    pod:tokenSecret "Nt6MePwumLrGHyuVl1ctPSAZVgg5GUNx3HtJX45PBhc" .
```

`pod:tokenSecret` is the plaintext bearer token to hand to the colleague. It is
surfaced **exactly once** — only its hash is stored. Capture it, and the record
id from `location`, before moving on:

```sh
export TOKEN=<the pod:tokenSecret value>
export RECORD=<the last path segment of location>
```

## 5. Owner: attach a policy

The record's `pod:policyRef` names a policy resource; writing it bounds the
grant — here: dead on August 1, at most 100 deliveries, at least 5 seconds
between them:

```sh
curl -X PUT "http://localhost:8000/.system/tokens/policies/$RECORD" \
  -H "Authorization: Bearer $ADMIN" -H "Content-Type: text/turtle" \
  --data '
@prefix pod: <https://lukasberka.github.io/ldp-personal-store/vocab#> .
<> a pod:Policy ;
   pod:expiresAt     "2026-08-01T00:00:00Z" ;
   pod:maxRetrievals 100 ;
   pod:minInterval   5 .'
```

The share is live. Everything the owner did was six `PUT`s and a `POST`.

## 6. Consumer: discover and fetch

You are now the colleague. You hold `$TOKEN`, know the pod's address, and
nothing else. Ask what it unlocks (UC6):

```sh
curl http://localhost:8000/.engine/discovery -H "Authorization: Bearer $TOKEN"
```

The response is an LDP container describing both views — titles, descriptions,
and their typed parameters: everything needed to build the next request, and
nothing about the owner's data or query internals. Fetch the "next two weeks":

```sh
curl -G http://localhost:8000/.engine/views/schedule-window \
  -H "Authorization: Bearer $TOKEN" \
  --data-urlencode "calendar=http://localhost:8000/calendar/work/" \
  --data-urlencode "from=2026-07-05" --data-urlencode "to=2026-07-19"
```

```turtle
<http://localhost:8000/.engine/blob/schedule-window?uri=...sprint-review&...>
    cal:date "2026-07-10" ;
    dct:title "Sprint review" .

<http://localhost:8000/.engine/blob/schedule-window?uri=...standup&...>
    cal:date "2026-07-06" ;
    dct:title "Team standup" .
```

Read what is *not* there: no `cal:privateNote`, no `cal:attendee`, no
`cal:location` — the view never selected them — and no *Quarterly planning
offsite*, whose August date falls outside the requested window. The subjects
are not raw storage URIs either: the engine rewrote them into gated
`/.engine/blob/` URLs, the only form of reference a consumer can follow.

## 7. The boundaries, demonstrated

Each denial below is the system enforcing a line the design draws.

**The policy meters use.** Repeat the fetch immediately — the 5-second minimum
interval has not elapsed:

```sh
curl -sS -w "\nHTTP %{http_code}\n" -G http://localhost:8000/.engine/views/schedule-window \
  -H "Authorization: Bearer $TOKEN" \
  --data-urlencode "calendar=http://localhost:8000/calendar/work/" \
  --data-urlencode "from=2026-07-05" --data-urlencode "to=2026-07-19"
```

```
{"detail":"policy: min interval not elapsed"}
HTTP 403
```

**The storage surface is closed to consumers.** The grant reads through views
only; the data plane answers `401` to it everywhere:

```sh
curl -sS -o /dev/null -w "HTTP %{http_code}\n" \
  http://localhost:8000/calendar/work/standup -H "Authorization: Bearer $TOKEN"
# HTTP 401
```

**Parameters are validated.** A value that does not match its declared type is
refused with `422` and a message naming the parameter; an omitted parameter
simply leaves its query variable unbound, so the result is not narrowed on
that axis.

## 8. Owner: audit and revoke

Every delivery was recorded (UC7). The statistics aggregate per view and per
grant — note that denied requests were not deliveries:

```sh
curl http://localhost:8000/.engine/stats -H "Authorization: Bearer $ADMIN"
```

```json
{"total": 2,
 "by_view":  [{"view_uri": ".../.system/views/schedule-window", "count": 1, ...}, ...],
 "by_token": [{"token_uri": ".../.system/tokens/WnfAUkmfeHo", "count": 2}]}
```

Individual entries are LDP resources under `/.system/access-log/`. And when the
share should end, revocation is one `DELETE` — no key rotation, no redeploy:

```sh
curl -X DELETE "http://localhost:8000/.system/tokens/$RECORD" \
  -H "Authorization: Bearer $ADMIN"

curl -sS -o /dev/null -w "HTTP %{http_code}\n" \
  http://localhost:8000/.engine/discovery -H "Authorization: Bearer $TOKEN"
# HTTP 401 — effective immediately
```

## The same flow in the browser

The [test console](../testing_client/README.md) drives this identical flow
form-by-form: the **Data** tab writes the events, **Views** composes and
installs the view (with a Turtle preview of exactly what will be sent),
**Grants** issues the token and hands the one-time secret straight into the
consumer role, and **Discover & read** renders each unlocked view as a card
with a typed parameter form. Under the hood it sends the same requests you
just made.

## Shortcut

`ADMIN=devtoken ./test_data/seed.sh` performs this walkthrough's owner side —
plus the four other scenarios — in one idempotent run, minting fresh consumer
tokens into `test_data/tokens.env`. Use it when you want a populated pod
without the ceremony; the point of this page was the ceremony.
