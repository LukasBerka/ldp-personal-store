#!/usr/bin/env python3
"""Bearer-injecting reverse proxy for running the W3C LDP Test Suite.

The pod gates *every* route behind ``Authorization: Bearer <admin-token>`` (reads
included), but the LDP test suite can only attach HTTP Basic credentials. Rather
than adding a test-only auth bypass to the server under test, this proxy sits in
front of the pod unchanged and stamps the bearer header onto every forwarded
request:

    ldp-testsuite  ->  this proxy (:9000)  ->  pod (:8000)

It is a plain stdlib forwarder (no third-party deps), streams nothing fancy, and
recomputes ``Content-Length`` after buffering each side's body — fine for the small
resources the suite creates. Configuration is entirely by environment variable so
``run.sh`` can wire it without arguments.
"""

import http.client
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PROXY_HOST = os.environ.get("PROXY_HOST", "0.0.0.0")
PROXY_PORT = int(os.environ.get("PROXY_PORT", "9000"))
UPSTREAM_HOST = os.environ.get("UPSTREAM_HOST", "127.0.0.1")
UPSTREAM_PORT = int(os.environ.get("UPSTREAM_PORT", "8000"))
TOKEN = os.environ.get("LDP_ADMIN_TOKEN", "")

# Headers that are connection-specific and must never be forwarded verbatim in
# either direction; Content-Length is dropped because we recompute it from the
# fully-buffered body.
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


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "ldp-auth-proxy/1.0"

    def log_message(self, fmt: str, *args: object) -> None:  # noqa: A002 - stdlib signature
        sys.stderr.write("[proxy] " + (fmt % args) + "\n")

    def _forward(self) -> None:
        body_len = int(self.headers.get("Content-Length", "0") or "0")
        body = self.rfile.read(body_len) if body_len else b""

        headers = {
            key: value
            for key, value in self.headers.items()
            if key.lower() not in _HOP_BY_HOP
        }
        headers["Authorization"] = f"Bearer {TOKEN}"
        headers["Host"] = f"{UPSTREAM_HOST}:{UPSTREAM_PORT}"

        conn = http.client.HTTPConnection(UPSTREAM_HOST, UPSTREAM_PORT, timeout=60)
        try:
            conn.request(self.command, self.path, body=body, headers=headers)
            upstream = conn.getresponse()
            payload = upstream.read()

            self.send_response_only(upstream.status, upstream.reason)
            for key, value in upstream.getheaders():
                if key.lower() in _HOP_BY_HOP:
                    continue
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            # HEAD has no message body even though the upstream reports a length.
            if self.command != "HEAD":
                self.wfile.write(payload)
        except (http.client.HTTPException, OSError) as exc:
            self.send_error(502, f"Bad gateway: {exc}")
        finally:
            conn.close()

    # Every LDP verb routes through the same forwarder.
    do_GET = _forward
    do_HEAD = _forward
    do_POST = _forward
    do_PUT = _forward
    do_DELETE = _forward
    do_OPTIONS = _forward
    do_PATCH = _forward


def main() -> None:
    if not TOKEN:
        sys.exit("LDP_ADMIN_TOKEN must be set for the auth proxy")
    server = ThreadingHTTPServer((PROXY_HOST, PROXY_PORT), _Handler)
    sys.stderr.write(
        f"[proxy] listening on {PROXY_HOST}:{PROXY_PORT} -> "
        f"{UPSTREAM_HOST}:{UPSTREAM_PORT} (injecting Bearer token)\n"
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
