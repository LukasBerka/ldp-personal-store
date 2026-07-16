# Demo — the use case scenarios, end to end

Here we introduce seven different use cases (UC1–UC7): sharing a
calendar without leaking private notes, giving a family member a photo album,
sharing lecture notes with a classmate, keeping a shopping list in sync with a
partner, and publishing a reading list for a fixed review period — plus
discovery and access monitoring on top. This directory shows each of them
**running**, as copy-paste commands with the expected responses, so you can see
in minutes what the pod does and why.

Everything here uses two common tools: `curl` for the owner's side, and
optionally a browser (the bundled [test console](../testing_client/README.md))
for the consumer's side. There is no other client to install — the pod's entire
interface is plain HTTP.

## The five-minute tour

Prerequisites: [uv](https://docs.astral.sh/uv/), `curl`, and two terminals,
both at the repository root.

```sh
# terminal 1 — run the pod with a known admin token
LDP_ADMIN_TOKEN=devtoken uv run python -m ldp_personal_store.main

# terminal 2 — load all five scenarios: data, views, grants, policies
ADMIN=devtoken ./test_data/seed.sh
. test_data/tokens.env    # exports COLLEAGUE_TOKEN, FAMILY_TOKEN, ...
```

You are now the *data consumer*. Ask the pod what your grant unlocks — no URIs,
no documentation, just the token you were handed (UC6):

```sh
curl http://localhost:8000/.engine/discovery \
  -H "Authorization: Bearer $COLLEAGUE_TOKEN"
```

The response lists the two calendar views the colleague grant unlocks, with
their titles, descriptions, and declared parameters. Fetch one:

```sh
curl -G http://localhost:8000/.engine/views/schedule-window \
  -H "Authorization: Bearer $COLLEAGUE_TOKEN" \
  --data-urlencode "calendar=http://localhost:8000/calendar/work/" \
  --data-urlencode "from=2026-07-05" --data-urlencode "to=2026-07-19"
```

Back come the work events of that fortnight — titles and dates only. The
private notes, attendee lists, and locations stored with each event never
leave the pod, and neither do the events outside the window.

Now switch roles. As the *owner*, see that use being recorded (UC7):

```sh
curl http://localhost:8000/.engine/stats -H "Authorization: Bearer devtoken"
```

That is the whole model: the owner stores data and defines **views**, hands a
consumer one **grant** token bounded by a **policy**, and the consumer reads
through the engine surface — never the data itself.

## The scenario walkthroughs

Each page is self-contained, states what the scenario demonstrates, and shows
the actual responses you should see.

| Page | Scenario | What it demonstrates |
|---|---|---|
| [uc1-calendar.md](uc1-calendar.md) | **Build the calendar share by hand** — the full owner → consumer flow, step by step | Creating containers and resources, authoring a parameterized view, issuing a grant, attaching a policy, consuming, denial, revocation |
| [uc2-photos.md](uc2-photos.md) | Photo album for a family member | Binary resources behind gated links: the consumer streams real PNGs without ever touching the storage surface; private GPS metadata withheld |
| [uc3-notes.md](uc3-notes.md) | Lecture notes for a classmate | Hierarchical organization as plain LDP containers, a share scoped to one branch of the tree, and what a consumer-supplied parameter does and does not decide |
| [uc4-shopping.md](uc4-shopping.md) | Household shopping list | A long-lived share of frequently changing data: the owner edits, the partner re-fetches, nothing to reconfigure — plus a single-use grant hitting its ceiling |
| [uc5-reading-list.md](uc5-reading-list.md) | Reading list for a review period | Time-boxed access: validity window, retrieval budget, rate limit, and the owner tightening a policy live |

UC1 is the one to start with: can you just create a calendar, insert some
events, create a view, and share it? Yes — that page does exactly that, on a
fresh pod, by hand.

### UC6, UC7 — covered along the way

**UC6 (discovery)** opens every walkthrough: the same
`/.engine/discovery` request lists *exactly* what the presented token unlocks —
try it with `$HOUSEHOLD_TOKEN` and only the shopping list appears.

**UC7 (monitoring)** closes them: `/.engine/stats` aggregates deliveries per
view and per grant, and each delivery is an individual LDP resource under
`/.system/access-log/`.

## The consumer in a browser

The consumer's side of every scenario can also be driven from the bundled
[test console](../testing_client/README.md) — a dependency-free static page:

```sh
cd testing_client && python3 -m http.server 5500   # → http://localhost:5500
```

Switch the role toggle to **Consumer**, paste a token from
`test_data/tokens.env`, and **Discover & read** renders each unlocked view as a
card with a typed form for its parameters; results list any referenced
resources (the UC2 photos) as download buttons. The **Owner** role drives the
management side — data, views, grants, policies, SPARQL, stats — with the admin
token, including the one-time grant-secret hand-off to the consumer role.

## Further

- [`test_data/README.md`](../test_data/README.md) — the seeded dataset in
  detail, and per-scenario checks of what a correct pod must return.
- [`penny/README.md`](../penny/README.md) — the pod browsed with **Penny**, a
  third-party Solid/LDP data browser the project did not write, as evidence
  that the surface is standard.
- The running pod's own API documentation, served at
  `http://localhost:8000/docs`, is the normative reference for every request
  these pages make.
