# Test Console — a zero-dependency client for the Personal LDP Pod

A single static web app for driving a running pod's HTTP API, split into the two
roles the pod is built around:

- **Owner** — set the pod up: write data, define parameterized SPARQL `CONSTRUCT`
  **views**, issue **grants** (consumer bearer tokens), bound them with **policies**,
  run **SPARQL**, and read delivery **stats**.
- **Consumer** — read a shared slice: **discover** the views a grant unlocks, fetch
  their results, and download the binaries they reference — through the engine
  surface only.

It is a testing/demo tool, not part of the product and not something the pod ships.

## Running it

No build, no dependencies, no server needed. Either:

```sh
# just open the file
xdg-open index.html          # or double-click it / open in any browser

# ...or serve it (identical result)
python3 -m http.server 5500  # then visit http://localhost:5500
```

Then, at the top of the page:

1. **Base URL** — where the pod listens (default `http://localhost:8000`).
2. **Check** — pings `GET /health`; the pill turns green with the version when reachable.
3. **Owner / Consumer** — the role switch. It selects which token and which set of
   tabs are in play.

Start a pod to point it at:

```sh
LDP_ADMIN_TOKEN=<secret> uv run python -m ldp_personal_store.main
```

The pod's CORS policy already allows the headers this client sends
(`Authorization`, `Content-Type`, `Accept`, `If-Match`, `If-None-Match`, `Slug`)
and exposes `ETag`/`Location`, so a browser opened from `file://` or any origin
can talk to it with no extra configuration.

## Owner walkthrough

Paste your **admin token** into the credentials strip, then:

- **Data** — browse any path (container members are clickable; the current `ETag`
  is captured automatically for edits), then `PUT`/`POST` RDF or upload a binary.
  Replacing an existing resource uses the captured `ETag` as `If-Match`.
- **Views** — fill the form (title, description, content-type hint, optional max
  retrievals, and typed parameters), **Preview Turtle** to see exactly what will be
  sent, then **Create view**. Existing views are listed with their parameter shapes.
- **Grants** — check the views to unlock and **Issue grant**. The one-time
  `pod:tokenSecret` is shown in a highlighted box with **Copy** and **Use as
  consumer token** (which drops it straight into the Consumer role). It is shown
  **once** and can never be retrieved again.
- **Policies** — a grant's issuance surfaces a policy id; bound the grant with any
  subset of expiry, validity window, total-retrieval ceiling, and minimum interval.
- **SPARQL** — read-only queries; `SELECT`/`ASK` render as a table/boolean,
  `CONSTRUCT` as text. Injection-safe variable bindings and the `.system/` scope
  toggle are supported.
- **Stats** — total deliveries plus per-view and per-grant breakdowns.

## Consumer walkthrough

Paste the **consumer token** the owner gave you (or use the bridge from the Grants
tab), then:

- **Discover & read** — **Load views** lists exactly what your grant unlocks. Each
  card has a typed parameter form; **Fetch result** runs the view. Any shared
  resources in the result (including binaries) appear as **Download** buttons that
  dereference the gated `/.engine/blob/…` proxy URLs for you.
- **Manual** — fetch a view by id, or paste a blob URL to download it directly.

## Notes

- The **Log** button (top right) opens a drawer recording every request and
  response — method, timing, status, headers, and body. Bearer tokens are never
  logged.
- Base URL and both tokens persist in `localStorage`; the ✕ buttons forget a token,
  and 👁 reveals it.
- All RDF is exchanged as Turtle. The client parses responses with a small built-in
  Turtle/N-Triples parser (`rdf.js`) and builds request bodies with correct literal
  escaping — no external RDF library.

## Files

| File | What it holds |
|---|---|
| `index.html` | Page structure and all controls |
| `styles.css` | Styling (light/dark aware) |
| `rdf.js` | Turtle/N-Triples parser + Turtle builders + vocabulary IRIs |
| `client.js` | Connection state, the single `fetch` entry point, request log |
| `app.js` | UI wiring and rendering for every panel |
