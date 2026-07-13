# Test data for the thesis use cases (UC1–UC7)

## Quick start

```bash
# terminal 1 — run the pod with a known admin token
LDP_ADMIN_TOKEN=devtoken uv run python -m ldp_personal_store.main

# terminal 2 — load everything, mint tokens, attach policies, log demo fetches
ADMIN=devtoken ./test_data/seed.sh
. test_data/tokens.env       # exports COLLEAGUE_TOKEN, FAMILY_TOKEN, ...
```

`BASE=https://pod.example.org ADMIN=... ./test_data/seed.sh` retargets another
pod (the base URI baked into the `.ttl` files is rewritten on the fly).
Resources and views are PUT with fixed URIs, so re-running is idempotent —
except that each run mints a fresh set of consumer tokens (old ones remain
valid until deleted via `DELETE /.system/tokens/<record-id>`).

## What gets created

| UC | Scenario | Data | View(s) | Token | Policy |
|----|----------|------|---------|-------|--------|
| UC1 | Calendar summary for a colleague | 8 events in `/calendar/work/` and `/calendar/personal/`, each with private `cal:privateNote` + `cal:attendee` | `schedule` (ch7 example, verbatim), `schedule-window` (adds `from`/`to`) | `COLLEAGUE_TOKEN` (unlocks both — FR4) | expires 2026-08-01, max 100 retrievals, 5 s interval |
| UC2 | Album for a family member | 3 PNG binaries + metadata resources (with private GPS coords) in `/photos/summer-2026/` | `album` (param `album`) | `FAMILY_TOKEN` | none (trusted) |
| UC3 | Course folder for a classmate | 5 notes under `/notes/linear-algebra/` and `/notes/algorithms/` | `lecture-notes` (param `lecture`) | `CLASSMATE_TOKEN` | expires 2026-09-30 |
| UC4 | Household shopping list | `/shopping/list` with 6 items; `list-week2.ttl` is the next week's state | `shopping-list` (no params) | `HOUSEHOLD_TOKEN` (shared by both partners) | none (long-lived, per ch4) |
| UC5 | Curated reading list for a review period | 5 bibliography entries in `/reading-list/`, 4 tagged `pods-review-2026`, all with private ratings/notes | `reading-list` (param `tag`, per-view ceiling 500) | `REVIEW_GROUP_TOKEN` | window 2026-07-05 .. 2026-08-15, max 200, 5 s interval |
| UC6 | Discovering available shares | — | `GET /.engine/discovery` with any consumer token | | |
| UC7 | Monitoring access | seed.sh performs 7 demo fetches so the log is non-empty | `GET /.engine/stats` with the admin token; raw log under `/.system/access-log/` | | |

## Per-scenario checks (what a correct pod must return)

**UC1** — `schedule-window` with `calendar=$BASE/calendar/work/`,
`from=2026-07-05`, `to=2026-07-19` returns exactly *Team standup* (07-06),
*Sprint review* (07-10) and *Client call: Q3 roadmap* (07-16). *Quarterly
planning offsite* (08-03) is outside the window; personal events belong to the
other calendar. No `cal:privateNote`, `cal:attendee`, `cal:location` or
`cal:start` triple may appear in any result. Unparameterized `schedule` on the
personal calendar returns all 4 personal events including the September concert.

**UC2** — `album` with `album=$BASE/photos/summer-2026/` returns 3 photos with
titles and capture dates, and **no** `pho:gpsLat`/`pho:gpsLong`. The photo
subjects are rewritten to `/.engine/blob/album?uri=...` URLs; fetching one with
`FAMILY_TOKEN` streams a valid PNG (`file` says "PNG image data, 96 x 64").
Fetching the raw storage URI `/photos/summer-2026/beach.png` with the consumer
token must yield 401.

**UC3** — `lecture-notes` with `lecture=$BASE/notes/linear-algebra/` returns the
3 linear-algebra notes with full text; the 2 algorithms notes are absent.
`GET /notes/` with the admin token shows the hierarchy via `ldp:contains`.

**UC4** — `shopping-list` returns 6 items with name/quantity/category/done.
To simulate the week's churn:
```bash
sed "s|http://localhost:8000|$BASE|g" test_data/uc4-shopping/list-week2.ttl | \
  curl -X PUT "$BASE/shopping/list" -H "Authorization: Bearer $ADMIN" \
       -H "Content-Type: text/turtle" --data-binary @-
```
then re-fetch: milk/tomatoes are done, bread and dish soap are gone, butter and
penne appear — same token, no reconfiguration.

**UC5** — `reading-list` with `tag=pods-review-2026` returns 4 entries
(Berners-Lee 2001, Sambra 2016, Speicher 2015, Verborgh 2016) with
title/creator/year/venue only — no `bib:rating`, no `bib:privateNote`, and no
*Clean Code* (untagged). Outside the 2026-07-05..2026-08-15 window, or after
200 retrievals, the same request answers 403 with the violated constraint named.

**UC6** — `/.engine/discovery` with `COLLEAGUE_TOKEN` lists exactly `schedule`
and `schedule-window` (titles, descriptions, declared parameters); with
`HOUSEHOLD_TOKEN` it lists only `shopping-list`. Nothing about other views or
query internals leaks.

**UC7** — `/.engine/stats` with the admin token reports the 7 seeded deliveries
with per-view and per-token breakdowns (`shopping-list` has 2, one shared
token). Each fetch also appears as an entry under `/.system/access-log/`.

Negative checks that hold everywhere: a consumer token gets 401 on the storage
surface (e.g. `GET /calendar/work/standup`), 403 on a view it is not linked to
(e.g. `FAMILY_TOKEN` on `schedule`), and 422 on a missing/ill-typed parameter
(e.g. `schedule` without `calendar`).

## Layout

Each `uc*/` directory holds the LDP resource bodies (`*.ttl`, absolute URIs
under `http://localhost:8000/`), the view definition(s) (`view-*.ttl`, posted
to `/.system/views/`), and the grant policy (`policy-*.ttl`, PUT to
`/.system/tokens/policies/<record-id>`). `uc2-photos/*.png` are small real
PNGs generated for the album. `tokens.env` is produced by `seed.sh` and is
git-ignored — it contains the plaintext bearer secrets.
