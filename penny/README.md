# penny — browse the pod with the Penny GUI

Lets the [**Penny**](https://penny.vincenttunru.com/) Solid/LDP data browser — a real,
third-party GUI client — drive this pod, despite the pod using a *static bearer token*
instead of Solid-OIDC (which Penny's login expects). It's a testing shim, in the same
spirit as `../w3c_ldp_test_suite/` (and its `auth_proxy.py`); it is not part of the
shipped server and is excluded from ruff.

This folder holds `proxy.py` (the auth-injecting reverse proxy) and instructions for
running the Penny UI itself locally. `proxy.py` sits in front of the pod and:

1. stamps `Authorization: Bearer <token>` onto **credential-less** requests, so Penny
   browses as the pod owner (default) or as a chosen consumer, with **no login** — while a
   client that sends its own bearer (e.g. `../test_data/seed.sh`) is passed through
   unchanged; and
2. normalises **CORS** and answers the browser's preflight — including the
   `Access-Control-Allow-Private-Network: true` header Chrome requires for a
   public HTTPS page (hosted Penny) calling `http://localhost`.

```
Penny (browser)  ->  proxy.py (:9000)  ->  pod (:8000)
```

## 1. Start the pod + proxy

**Quickest — one command** (from this directory; starts pod + proxy, Ctrl-C stops both):

```sh
./run.sh                    # OWNER mode  — browse the data plane as the pod owner
./run.sh <consumer-token>   # CONSUMER mode — browse the /.engine/ surface as that grant
```

`run.sh` prints the URL to point Penny at for the chosen mode. In **consumer** mode the
pod still boots with its admin token; only the bearer the proxy injects changes, and the
entry point is the engine discovery document (a consumer gets `401` on the data-plane root,
by design). Get a consumer token from an issued grant — e.g. one printed by
`../test_data/seed.sh`, or from `POST /.system/tokens`.

| Mode | Command | Point Penny at |
|---|---|---|
| Owner | `./run.sh` | `http://localhost:9000/` |
| Consumer | `./run.sh <token>` | `http://localhost:9000/.engine/discovery` |

**Or manually, in two terminals:**

1. Start the pod with its base URI pointed at the proxy (from the repository root, i.e.
   the parent of this directory):

   ```sh
   LDP_ADMIN_TOKEN=dev-secret LDP_BASE_URI=http://localhost:9000/ \
     uv run python -m ldp_personal_store.main
   ```

   > `LDP_BASE_URI=http://localhost:9000/` is **required**, not cosmetic. The pod mints
   > every resource URI, `Location`, and `ldp:contains` link from it. If left at `:8000`,
   > the links Penny shows point straight at the pod and skip the proxy → `401`. Pointed at
   > the proxy, every dereference stays authorised.

2. Start the proxy (from this directory). `INJECT_TOKEN` is the bearer stamped onto
   credential-less requests — the admin token for owner browsing, or a grant token for
   consumer browsing:

   ```sh
   INJECT_TOKEN=dev-secret python3 proxy.py
   ```

   (Env knobs: `PROXY_PORT` 9000, `UPSTREAM_HOST` 127.0.0.1, `UPSTREAM_PORT` 8000.
   `INJECT_TOKEN` falls back to `LDP_ADMIN_TOKEN` if unset.)

## 2. Run the Penny UI

Penny is a client-side app: the browser that loads it makes the `fetch()` calls, so
`localhost` means *your* machine.

Penny is a Next.js app; clone and run its dev server (the clone is gitignored under `app/`):

```sh
# from this directory
git clone --depth 1 https://gitlab.com/vincenttunru/penny.git app
cd app
npm install        # Penny declares Node 16/18/20; newer Node works with EBADENGINE warnings
npm run dev        # Next.js dev server on http://localhost:3000
```

Then open **http://localhost:3000**, and in Penny's **"inspect a URL"** box paste the
entry URL for your mode:

- Owner: `http://localhost:9000/`
- Consumer: `http://localhost:9000/.engine/discovery`

Navigate the container, open resources, follow links. (Do *not* type the pod URL into the
browser's own address bar — that bypasses Penny and just shows raw Turtle.)

## Populate with the thesis test data (optional)

To browse the UC1–UC5 dataset (calendars, photos, notes, shopping, reading list) instead
of an empty pod, seed it **through the proxy** so every URI is minted at the proxy origin:

```sh
BASE=http://localhost:9000 ADMIN=dev-secret ../test_data/seed.sh
```

(The proxy passes `seed.sh`'s own admin/consumer bearer tokens through untouched, so its
per-token demo fetches still work; only credential-less requests — Penny's — get the token
injected.)
