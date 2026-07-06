# LDP conformance testing

Runs the official [W3C LDP Test Suite](https://github.com/w3c/ldp-testsuite)
against this pod and produces [EARL](https://www.w3.org/TR/EARL10-Schema/)
conformance reports.

## TL;DR

```sh
w3c_ldp_test_suite/run.sh
```

First run builds two Docker images and downloads the suite's Maven dependencies
(a few minutes). Each leg gets its own subdirectory under `w3c_ldp_test_suite/reports/`
(gitignored) because the suite writes a fixed report filename per run:

| Path | What it is |
| --- | --- |
| `reports/basic/report/ldp-testsuite-execution-report-earl.ttl` | EARL results, Basic Container + LDP-NR |
| `reports/direct/report/ldp-testsuite-execution-report-earl.ttl` | EARL results, Direct Container |
| `reports/{basic,direct}/report/…-earl.jsonld` | Same, JSON-LD |
| `reports/{basic,direct}/report/…-report.html` | Human-readable HTML report |
| `reports/{basic,direct}/test-output/` | Full TestNG output (per-test XML/HTML) |
| `reports/{basic,direct}.log` | Console output for the run |

A run prints a summary line like `Total tests run: 112, Failures: 19, Skips: 35`.

## Architecture

Everything runs in containers on one Docker network — no host-networking
assumptions, so it behaves identically under Docker Desktop/WSL2 or a native
engine:

```
suite (Docker)  ──►  proxy :9000  ──►  pod :8000
   --server           +Authorization: Bearer <admin-token>
   http://proxy:9000/
```

- **pod** — built from the repo `Dockerfile`, unchanged. Binds `0.0.0.0:8000`
  (`LDP_TLS_MODE=terminated`) and mints resource URIs under `http://proxy:9000/`.
- **proxy** (`auth_proxy.py`) — a dependency-free stdlib reverse proxy that stamps
  `Authorization: Bearer <admin-token>` onto every request.
- **suite** — built from `w3c_ldp_test_suite/Dockerfile`, talks only to `proxy`.

### Why the proxy exists

Every pod route requires a bearer token — reads included. The suite can only
attach HTTP **Basic** credentials (`--auth user:pass`), never a bearer token.
Rather than weaken the server under test with an auth bypass, the pod runs
**unchanged** behind the proxy.

### Why URIs point at the proxy

The suite follows `Location` headers the pod returns. Because the pod mints URIs
under `LDP_BASE_URI=http://proxy:9000/` (a value independent of its bind
interface), every followed link stays on the authenticated path. The pod reaching
`0.0.0.0` with `tls_mode=terminated` satisfies its plaintext-TLS precondition.

## What is and isn't tested

The suite selects **one** container type per run and POSTs into the `--server`
URL, so the harness runs it twice:

- **Basic** against the pod root `/` (seeded as an `ldp:BasicContainer`), with
  `--non-rdf` for the LDP Non-RDF Source (binary) group.
- **Direct** against a `tck-direct/` container the harness PUTs first.
- **Indirect** is intentionally skipped — the pod implements only Basic and Direct
  Containers (`app/ldp/containers.py`). Report this as an unimplemented feature,
  not a failure.

## Expected findings (not harness bugs)

Some `MUST`/`SHOULD` assertions are expected to fail because the pod does not
implement those parts of LDP. These are legitimate conformance findings, e.g.:

- No `Accept-Post` header on container responses (LDP MUST for POST support).
- No `PATCH` / `Accept-Patch` support.
- No paging.

Distinguish these (stable, explained by the code) from harness breakage (a whole
run erroring out, connection refused, or 401s everywhere → the proxy/token wiring).

## Isolation & safety

- The pod runs against a fresh in-container `/data` (no named volume), never your
  local `./data`.
- The admin token is generated per run (override by exporting `LDP_ADMIN_TOKEN`).
- `run.sh` tears the whole stack down on exit (`docker compose down -v`).

## Manual / debugging use

```sh
export LDP_ADMIN_TOKEN=dev-token
docker compose -f w3c_ldp_test_suite/docker-compose.yml up -d --wait pod proxy
# proxy is published on 127.0.0.1:9000 for ad-hoc requests:
curl -H "Authorization: Bearer $LDP_ADMIN_TOKEN" http://localhost:9000/
# run just one leg:
docker compose -f w3c_ldp_test_suite/docker-compose.yml run --rm suite \
  --server http://proxy:9000/ --basic --output /out --earl /out/earl-basic.ttl
docker compose -f w3c_ldp_test_suite/docker-compose.yml down -v
```

## How the suite is built

`Dockerfile` fetches the suite pinned to commit
`8e7936888c619b03ed2caffeed9e26b151347ac0` and builds its shaded jar under **JDK 8**
(the source targets Java 1.7 with 2017-era deps; the host's Java 21 can neither
compile nor cleanly run it). The upstream repo is archived, so the pin keeps
rebuilds reproducible.
