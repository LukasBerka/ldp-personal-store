#!/usr/bin/env python3
"""Bearer-injecting reverse proxy that lets the Penny Solid/LDP GUI drive the pod.
This proxy sits in front of the pod and:

stamps ``Authorization: Bearer <token>`` (which sends no credential) browses as whichever identity that token names —
the pod owner by default (``LDP_ADMIN_TOKEN``), or a consumer if ``INJECT_TOKEN`` is
a grant token. A client that sends its own bearer (e.g. ``test_data/seed.sh``, which
uses distinct admin and consumer tokens) is passed through unchanged

Penny (browser)  ->  this proxy (:9000)  ->  pod (:8000)

IMPORTANT: run the pod with ``LDP_BASE_URI`` set to THIS proxy's origin
(e.g. ``http://localhost:9000/``). The pod mints every resource URI, ``Location`` header
and ``ldp:contains`` link from that base; if it stays at ``:8000`` the links Penny renders
point straight at the pod and bypass the injected auth (=> 401). Pointed at the proxy,
every dereference stays on the authorised path.
"""

import http.client
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PROXY_HOST = os.environ.get("PROXY_HOST", "127.0.0.1")
PROXY_PORT = int(os.environ.get("PROXY_PORT", "9000"))
UPSTREAM_HOST = os.environ.get("UPSTREAM_HOST", "127.0.0.1")
UPSTREAM_PORT = int(os.environ.get("UPSTREAM_PORT", "8000"))
# The bearer the proxy injects for credential-less (browser) requests. Defaults to the
# pod's admin token so Penny browses as the owner; set INJECT_TOKEN to a consumer grant
# token instead to browse the pod as that consumer (the /.engine/ surface).
TOKEN = os.environ.get("INJECT_TOKEN") or os.environ.get("LDP_ADMIN_TOKEN", "")

# Connection-specific headers that must never be forwarded verbatim in either direction;
# Content-Length is dropped because we recompute it from the fully-buffered body.
_HOP_BY_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailers",
        "transfer-encoding",
        "upgrade",
        "content-length",
        "host",
    }
)

_CORS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, HEAD, POST, PUT, DELETE, OPTIONS, PATCH",
    "Access-Control-Allow-Headers": (
        "Authorization, Content-Type, Slug, Link, Accept, Prefer, If-Match, If-None-Match"
    ),
    "Access-Control-Expose-Headers": (
        "ETag, Location, Link, Allow, Accept-Post, Preference-Applied, WWW-Authenticate, "
        "Content-Type"
    ),
    "Access-Control-Allow-Private-Network": "true",
    "Access-Control-Max-Age": "600",
}


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "penny-auth-proxy/1.0"

    def log_message(self, fmt: str, *args: object) -> None:  # noqa: A002 - stdlib signature
        sys.stderr.write("[penny-proxy] " + (fmt % args) + "\n")

    def _read_body(self) -> bytes:
        if "chunked" in self.headers.get("Transfer-Encoding", "").lower():
            body = b""
            while True:
                size_line = self.rfile.readline()
                if not size_line:
                    break
                size = int(size_line.strip().split(b";", 1)[0] or b"0", 16)
                if size == 0:
                    self.rfile.readline()  # consume the trailing CRLF
                    break
                body += self.rfile.read(size)
                self.rfile.readline()  # consume the CRLF after each chunk
            return body
        body_len = int(self.headers.get("Content-Length", "0") or "0")
        return self.rfile.read(body_len) if body_len else b""

    def _send_preflight(self) -> None:
        self.send_response_only(204, "No Content")
        for key, value in _CORS.items():
            self.send_header(key, value)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _forward(self) -> None:
        # Answer the browser's CORS preflight ourselves — never make it depend on the pod.
        request_headers = {key.lower() for key in self.headers}
        if self.command == "OPTIONS" and "access-control-request-method" in request_headers:
            self._send_preflight()
            return

        body = self._read_body()

        headers = {
            key: value
            for key, value in self.headers.items()
            if key.lower() not in _HOP_BY_HOP and not key.lower().startswith("access-control-")
        }
        if not any(key.lower() == "authorization" for key in headers):
            headers["Authorization"] = f"Bearer {TOKEN}"
        headers["Host"] = f"{UPSTREAM_HOST}:{UPSTREAM_PORT}"

        conn = http.client.HTTPConnection(UPSTREAM_HOST, UPSTREAM_PORT, timeout=60)
        try:
            conn.request(self.command, self.path, body=body, headers=headers)
            upstream = conn.getresponse()
            payload = upstream.read()

            self.send_response_only(upstream.status, upstream.reason)
            for key, value in upstream.getheaders():
                if key.lower() in _HOP_BY_HOP or key.lower().startswith("access-control-"):
                    continue
                self.send_header(key, value)
            for key, value in _CORS.items():
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            # HEAD carries no message body even though the upstream reports a length.
            if self.command != "HEAD":
                self.wfile.write(payload)
        except (http.client.HTTPException, OSError) as exc:
            self.send_error(502, f"Bad gateway: {exc}")
        finally:
            conn.close()

    do_GET = _forward
    do_HEAD = _forward
    do_POST = _forward
    do_PUT = _forward
    do_DELETE = _forward
    do_OPTIONS = _forward
    do_PATCH = _forward


def main() -> None:
    if not TOKEN:
        sys.exit("Set INJECT_TOKEN (or LDP_ADMIN_TOKEN) to the bearer the proxy injects")
    server = ThreadingHTTPServer((PROXY_HOST, PROXY_PORT), _Handler)
    sys.stderr.write(
        f"[penny-proxy] listening on http://{PROXY_HOST}:{PROXY_PORT} -> "
        f"{UPSTREAM_HOST}:{UPSTREAM_PORT} (injecting Bearer token, CORS enabled)\n"
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
