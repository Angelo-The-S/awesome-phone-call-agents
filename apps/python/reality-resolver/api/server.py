"""Read-only HTTP surface over the case catalog, on the standard library.

No framework: this app already serves HTTP with http.server in
fake_server.py, four endpoints do not justify a dependency tree, and
CONTRIBUTING asks contributions to install without one.

Scope of this file today, stated so the boundary is checkable rather
than assumed: it imports the case store and the serializers, and nothing
else from the app. It does not import pipeline, client, or any CALL-E
code path; it cannot place a call, cannot start a resolution, and needs
no credential to run. Importing this module starts nothing - the server
is created by create_server() and run by main().
"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlparse

from api.serialize import cases_payload, error_payload, health_payload
from api.store import CaseStore

# Loopback by default and on purpose. This server has no authentication,
# so binding it to every interface would be a decision an operator makes
# explicitly, never a default they inherit.
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000

# Mirrors pyproject.toml's version. Reported so a client can tell which
# engine answered; it is not a build identifier and carries no path.
ENGINE_VERSION = "0.0.0"

# There is no live mode to report yet: nothing in this server can reach a
# provider, so anything other than "fake" would be a claim the code does
# not back. The configuration seam belongs with the phase that can
# actually place a call.
MODE = "fake"

# Largest declared request body this server will read and discard before
# giving up on reusing the connection. No route reads input, so this only
# bounds how much is thrown away, never how much is processed.
MAX_DRAIN_BYTES = 1 << 20  # 1 MiB
DRAIN_CHUNK_BYTES = 64 * 1024


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    @property
    def store(self) -> CaseStore:
        return self.server.store  # type: ignore[attr-defined]

    def log_message(self, *args: Any) -> None:
        """Silent, same as fake_server.py. The default writes the request
        line to stderr, which would put every requested path into
        whatever collects that stream.
        """
        return

    def _send(self, status: int, body: dict[str, Any], extra_headers: dict[str, str] | None = None) -> None:
        raw = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        if self.close_connection:
            # Something upstream decided this connection cannot be reused
            # - a body that could not be drained, typically. Say so:
            # closing without the header leaves the client believing it
            # may send another request down a socket that is going away.
            self.send_header("Connection", "close")
        for name, value in (extra_headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(raw)

    def _path(self) -> str:
        return urlparse(self.path).path.rstrip("/") or "/"

    def _not_found(self) -> None:
        # Deliberately does not echo the requested path back: reflecting
        # client-controlled text into a response body is a habit worth
        # not starting, and the client already knows what it asked for.
        self._send(404, error_payload("not_found", "no route for this path"))

    def _method_not_allowed(self, allowed: str) -> None:
        self._send(
            405,
            error_payload("method_not_allowed", f"this endpoint accepts {allowed}"),
            {"Allow": allowed},
        )

    def _drain_request_body(self) -> None:
        """Discard the request body so the next request on a keep-alive
        connection is parsed from a request line rather than from
        leftover body bytes.

        Neither route reads input, and skipping this was a real defect
        rather than a theoretical one: under protocol_version HTTP/1.1
        the bytes stayed in the socket, and the following request on the
        same connection was parsed starting from them - answering with
        the stdlib's HTML 501 page, with the previous body quoted back
        inside it, instead of JSON.

        Only a declared Content-Length is read. With no Content-Length
        there is nothing to drain, and reading anyway would block until
        the peer gave up. A chunked body, an unparseable length, an
        oversized one, or a client that stops sending early all close
        the connection instead - correct, and cheaper than decoding a
        transfer encoding for input no route wants.
        """
        if "chunked" in self.headers.get("Transfer-Encoding", "").lower():
            self.close_connection = True
            return
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            return
        try:
            remaining = int(raw_length)
        except ValueError:
            self.close_connection = True
            return
        if remaining <= 0:
            return
        if remaining > MAX_DRAIN_BYTES:
            self.close_connection = True
            return
        while remaining > 0:
            chunk = self.rfile.read(min(remaining, DRAIN_CHUNK_BYTES))
            if not chunk:
                self.close_connection = True
                return
            remaining -= len(chunk)

    def _dispatch(self, method: str) -> None:
        # Before anything else, and on every path - 200, 404, 405 and
        # 500 all leave the connection reusable only if the body is gone.
        self._drain_request_body()
        path = self._path()
        if path not in ROUTES:
            # Route first, then method: an unknown path is 404 whatever
            # the verb, and 405 is reserved for a real endpoint.
            self._not_found()
            return
        handler = ROUTES[path].get(method)
        if handler is None:
            self._method_not_allowed(", ".join(sorted(ROUTES[path])))
            return
        try:
            status, body = handler(self)
        except Exception:
            # Nothing internal crosses the wire: no exception text, no
            # traceback, no file path. The operator's own logs are where
            # a failure gets diagnosed.
            self._send(500, error_payload("internal_error", "the server failed to handle this request"))
            return
        self._send(status, body)

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    # --- route handlers ------------------------------------------------

    def handle_health(self) -> tuple[int, dict[str, Any]]:
        return 200, health_payload(MODE, ENGINE_VERSION)

    def handle_cases(self) -> tuple[int, dict[str, Any]]:
        return 200, cases_payload(self.store.all())


ROUTES: dict[str, dict[str, Any]] = {
    "/api/health": {"GET": Handler.handle_health},
    "/api/cases": {"GET": Handler.handle_cases},
}


def create_server(
    host: str = DEFAULT_HOST, port: int = DEFAULT_PORT, store: CaseStore | None = None
) -> ThreadingHTTPServer:
    """Build a server without starting it. The caller decides whether to
    serve_forever() on it or drive it from a thread.
    """
    server = ThreadingHTTPServer((host, port), Handler)
    server.store = store or CaseStore()  # type: ignore[attr-defined]
    return server


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Read-only HTTP view of Reality Resolver's case catalog. Places no calls."
    )
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args(argv)

    server = create_server(args.host, args.port)
    host, port = server.server_address[:2]
    print(f"Reality Resolver API on http://{host}:{port} (mode={MODE}, no calls can be placed)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.", flush=True)
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
