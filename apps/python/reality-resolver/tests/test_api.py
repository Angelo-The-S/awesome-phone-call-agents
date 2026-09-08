"""Tests for the read-only API surface (api/).

Everything here runs against a server on a random loopback port, driven
with urllib from the standard library - the same shape as the rest of
this suite. No test reaches a provider, and in this phase the server has
no code path that could: it cannot start a resolution and imports no
CALL-E client.
"""

from __future__ import annotations

import http.client
import json
import re
import sys
import threading
import urllib.error
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from api.serialize import case_metadata
from api.store import CaseNotFoundError, CaseStore
from evidence.model import load_case

HERE = Path(__file__).resolve().parent.parent
LIVE_FIXTURE = "ghost-appointment-live-test"


@contextmanager
def api_server() -> Iterator[str]:
    from api.server import create_server

    server = create_server("127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.0005}, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    try:
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def request(base_url: str, path: str, method: str = "GET") -> tuple[int, Any, dict[str, str]]:
    req = urllib.request.Request(f"{base_url}{path}", method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            return response.status, json.loads(response.read()), dict(response.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read()), dict(exc.headers)


def walk_strings(value: Any) -> Iterator[str]:
    if isinstance(value, dict):
        for key, inner in value.items():
            yield str(key)
            yield from walk_strings(inner)
    elif isinstance(value, list):
        for item in value:
            yield from walk_strings(item)
    elif isinstance(value, str):
        yield value


# --- A. health -------------------------------------------------------


def test_health_returns_ok_and_nothing_else() -> None:
    with api_server() as base_url:
        status, body, headers = request(base_url, "/api/health")

        assert status == 200
        assert headers["Content-Type"] == "application/json"
        assert int(headers["Content-Length"]) > 0
        assert body["status"] == "ok"
        assert body["mode"] == "fake"
        assert body["engine_version"]
        # A health endpoint is where configuration classically leaks.
        assert set(body) == {"status", "mode", "engine_version"}


# --- B. cases --------------------------------------------------------


def test_cases_lists_both_shipped_cases_with_masked_numbers() -> None:
    with api_server() as base_url:
        status, body, _ = request(base_url, "/api/cases")

        assert status == 200
        names = [case["name"] for case in body["cases"]]
        assert "critical-service-escalation" in names
        assert "ghost-appointment" in names

        for case in body["cases"]:
            assert case["call_phone_masked"].startswith("+...")
            assert case["use_case"]
            assert case["deadline"].endswith("Z")
            assert case["decision_options"]
            assert case["evidence"]
            assert "call_phone" not in case
            assert "call_task_hint" not in case


def test_no_clear_phone_number_appears_anywhere_in_the_cases_response() -> None:
    """Checked against the real numbers read off disk, not a pattern:
    a regex could pass simply by being wrong.
    """
    real_numbers = {
        load_case(HERE / "cases" / f"{name}.json").call_phone
        for name in ("critical-service-escalation", "ghost-appointment")
    }
    with api_server() as base_url:
        _, body, _ = request(base_url, "/api/cases")

        raw = json.dumps(body)
        for number in real_numbers:
            assert number not in raw, "a case file's call_phone reached the client in the clear"
        assert not re.search(r"\+\d{7,15}", raw), "an unmasked E.164 number reached the client"


# --- C. the untracked live fixture -----------------------------------


def test_local_live_fixture_is_never_served_even_when_present_on_disk() -> None:
    """cases/ doubles as the operator's scratch directory for real-call
    testing, so exposure is opt-in by name. This is the test that would
    fail if the store ever started globbing the directory.
    """
    store = CaseStore()
    assert LIVE_FIXTURE not in store.names()
    with pytest.raises(CaseNotFoundError):
        store.get(LIVE_FIXTURE)

    with api_server() as base_url:
        _, body, _ = request(base_url, "/api/cases")
        assert LIVE_FIXTURE not in [case["name"] for case in body["cases"]]


def test_store_refuses_a_name_outside_the_allowlist(tmp_path: Path) -> None:
    """Including anything shaped like a traversal: the name is matched
    against a fixed tuple before it is ever used to build a path.
    """
    store = CaseStore()
    for name in ("../client", "../../README", "does-not-exist", ""):
        with pytest.raises(CaseNotFoundError):
            store.get(name)


# --- D/E/F. routing and methods --------------------------------------


def test_unknown_route_is_a_json_404_without_a_traceback() -> None:
    with api_server() as base_url:
        status, body, _ = request(base_url, "/api/nope")

        assert status == 404
        assert body["error"]["code"] == "not_found"
        assert "Traceback" not in json.dumps(body)
        assert "/api/nope" not in json.dumps(body), "the response should not reflect the path back"


def test_unknown_route_is_404_even_for_post() -> None:
    with api_server() as base_url:
        status, body, _ = request(base_url, "/api/resolutions", method="POST")

        assert status == 404, "no resolution endpoint exists in this phase"
        assert body["error"]["code"] == "not_found"


def test_post_to_health_is_405_with_an_allow_header() -> None:
    with api_server() as base_url:
        status, body, headers = request(base_url, "/api/health", method="POST")

        assert status == 405
        assert body["error"]["code"] == "method_not_allowed"
        assert headers["Allow"] == "GET"


def test_post_to_cases_is_405() -> None:
    with api_server() as base_url:
        status, body, headers = request(base_url, "/api/cases", method="POST")

        assert status == 405
        assert body["error"]["code"] == "method_not_allowed"
        assert headers["Allow"] == "GET"


def test_no_cors_header_is_sent_by_default() -> None:
    with api_server() as base_url:
        _, _, headers = request(base_url, "/api/health")

        assert "Access-Control-Allow-Origin" not in headers


# --- G. no credential required ---------------------------------------


def test_api_serves_without_any_calle_credential(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CALLE_API_KEY", raising=False)
    with api_server() as base_url:
        assert request(base_url, "/api/health")[0] == 200
        assert request(base_url, "/api/cases")[0] == 200


# --- H. anti-secret sweep --------------------------------------------


def test_no_response_leaks_a_credential_or_internal_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """A sentinel key is planted in the environment first: the point is
    not that the server has nothing to leak, it is that it leaks nothing
    even when something is there.
    """
    sentinel = "iams_live_sentinel_key_that_must_never_appear"
    monkeypatch.setenv("CALLE_API_KEY", sentinel)

    forbidden = (sentinel, "CALLE_API_KEY", "Authorization", "Bearer", "idempotency", "api.heycall-e.com")
    with api_server() as base_url:
        for path in ("/api/health", "/api/cases", "/api/nope"):
            _, body, _ = request(base_url, path)
            raw = json.dumps(body)
            for needle in forbidden:
                assert needle.lower() not in raw.lower(), f"{needle!r} leaked from {path}"
            for text in walk_strings(body):
                assert "C:\\" not in text and "/Users/" not in text, "a server path leaked"


# --- I. import safety ------------------------------------------------


def test_importing_the_server_starts_nothing_and_pulls_in_no_call_path() -> None:
    """The boundary this phase claims: HTTP cannot reach CALL-E, because
    the modules that can are not even loaded.
    """
    for name in [m for m in list(sys.modules) if m.startswith("api") or m in ("pipeline", "client")]:
        del sys.modules[name]

    import api.server  # noqa: F401

    assert "pipeline" not in sys.modules, "the HTTP layer must not import the pipeline in this phase"
    # api.serialize imports mask_phone from client, so client is expected;
    # what matters is that no client can be constructed from a route.
    import api.server as server_module

    source = Path(server_module.__file__).read_text(encoding="utf-8")
    for forbidden in ("CallEClient", "create_call", "poll_until_terminal", "resolve(", "from pipeline"):
        assert forbidden not in source, f"{forbidden} must not appear in the HTTP layer yet"


def test_create_server_does_not_serve_until_asked() -> None:
    from api.server import create_server

    server = create_server("127.0.0.1", 0)
    try:
        host, port = server.server_address[:2]
        assert port != 0
        # The socket is bound, so the OS completes the TCP handshake from
        # the listen backlog - but nothing is reading it, so the request
        # never gets a reply. Timing out is the evidence that no request
        # loop is running; a served port would answer immediately.
        with pytest.raises((urllib.error.URLError, TimeoutError, OSError)):
            urllib.request.urlopen(f"http://{host}:{port}/api/health", timeout=0.4)
    finally:
        server.server_close()


# --- serializer unit checks ------------------------------------------


def test_case_metadata_masks_the_number_and_omits_the_task_hint() -> None:
    case = load_case(HERE / "cases" / "critical-service-escalation.json")
    payload = case_metadata(case)

    assert payload["call_phone_masked"] == "+...0187"
    assert case.call_phone not in json.dumps(payload)
    assert "call_task_hint" not in payload
    assert payload["decision_options"]["if_confirmed"] == "CONTINUE_DISPATCH"
    assert payload["evidence"][0]["freshness_hours"] == 72


# --- keep-alive: a request body must not desync the connection -------
#
# urllib opens a fresh connection per request, so none of the tests above
# could see this. These use http.client directly and reuse one
# connection, which is what a browser - and therefore the web UI - does.


@contextmanager
def keepalive_connection(base_url: str) -> Iterator[http.client.HTTPConnection]:
    host, port = base_url.removeprefix("http://").split(":")
    connection = http.client.HTTPConnection(host, int(port), timeout=5)
    try:
        yield connection
    finally:
        connection.close()


def read(response: http.client.HTTPResponse) -> tuple[int, str, str]:
    body = response.read().decode("utf-8", "replace")
    return response.status, response.getheader("Content-Type") or "", body


PROBE = '{"probe":"MUST_NOT_BE_REFLECTED"}'


def test_post_body_then_get_on_the_same_connection_stays_clean() -> None:
    """The exact sequence that used to break: the 405 was correct, but
    the unread body made the next request parse from the wrong offset,
    producing an HTML 501 that quoted the body back at the client.
    """
    with api_server() as base_url, keepalive_connection(base_url) as connection:
        connection.request("POST", "/api/health", body=PROBE, headers={"Content-Type": "application/json"})
        status, content_type, body = read(connection.getresponse())
        assert status == 405
        assert content_type == "application/json"
        assert json.loads(body)["error"]["code"] == "method_not_allowed"

        connection.request("GET", "/api/health")
        status, content_type, body = read(connection.getresponse())

        assert status == 200, "the reused connection must still answer correctly"
        assert content_type == "application/json"
        payload = json.loads(body)
        assert payload["status"] == "ok"
        assert payload["mode"] == "fake"
        assert "MUST_NOT_BE_REFLECTED" not in body, "the previous body contaminated this response"
        assert "<html" not in body.lower()


@pytest.mark.parametrize(
    ("method", "path", "expected"),
    [
        ("POST", "/api/health", 405),
        ("POST", "/api/cases", 405),
        ("POST", "/api/nope", 404),
        ("GET", "/api/health", 200),
        ("GET", "/api/cases", 200),
    ],
)
def test_every_route_survives_a_body_on_a_reused_connection(method: str, path: str, expected: int) -> None:
    """All three outcomes were affected, not just the 405."""
    with api_server() as base_url, keepalive_connection(base_url) as connection:
        connection.request(method, path, body=PROBE, headers={"Content-Type": "application/json"})
        first_status, _, _ = read(connection.getresponse())
        assert first_status == expected

        connection.request("GET", "/api/health")
        status, content_type, body = read(connection.getresponse())

        assert status == 200
        assert content_type == "application/json"
        assert json.loads(body)["status"] == "ok"
        assert "MUST_NOT_BE_REFLECTED" not in body


def test_several_bodies_in_a_row_on_one_connection() -> None:
    with api_server() as base_url, keepalive_connection(base_url) as connection:
        for index in range(4):
            connection.request(
                "POST", "/api/cases", body=f'{{"n":{index},"pad":"{"x" * 500}"}}',
                headers={"Content-Type": "application/json"},
            )
            assert read(connection.getresponse())[0] == 405

        connection.request("GET", "/api/cases")
        status, content_type, body = read(connection.getresponse())
        assert status == 200
        assert content_type == "application/json"
        assert len(json.loads(body)["cases"]) == 2


def test_a_body_with_no_content_length_does_not_hang_the_request() -> None:
    """No Content-Length means nothing to drain. Reading anyway would
    block until the peer gave up, so the server must not try.
    """
    with api_server() as base_url, keepalive_connection(base_url) as connection:
        connection.putrequest("GET", "/api/health")
        connection.endheaders()  # no Content-Length, no body
        status, content_type, body = read(connection.getresponse())

        assert status == 200
        assert content_type == "application/json"
        assert json.loads(body)["status"] == "ok"


def test_a_chunked_body_closes_the_connection_instead_of_desyncing() -> None:
    """This server does not decode transfer encodings for input no route
    reads. Closing is the honest outcome: it never claims to have
    consumed a body it cannot delimit.
    """
    with api_server() as base_url, keepalive_connection(base_url) as connection:
        connection.putrequest("POST", "/api/health")
        connection.putheader("Transfer-Encoding", "chunked")
        connection.endheaders()
        connection.send(b"5\r\nhello\r\n0\r\n\r\n")
        response = connection.getresponse()
        status, content_type, body = read(response)

        assert status == 405
        assert content_type == "application/json"
        assert "MUST_NOT_BE_REFLECTED" not in body
        assert response.will_close, "a body this server cannot delimit must end the connection"


def test_a_malformed_content_length_closes_the_connection() -> None:
    with api_server() as base_url, keepalive_connection(base_url) as connection:
        connection.putrequest("GET", "/api/health")
        connection.putheader("Content-Length", "not-a-number")
        connection.endheaders()
        response = connection.getresponse()
        status, content_type, _ = read(response)

        assert status == 200
        assert content_type == "application/json"
        assert response.will_close


def test_an_oversized_declared_body_closes_rather_than_being_read() -> None:
    """The server declines to read a gigabyte it has no use for; it drops
    the connection instead of draining, and never allocates the body.
    """
    from api.server import MAX_DRAIN_BYTES

    with api_server() as base_url, keepalive_connection(base_url) as connection:
        connection.putrequest("POST", "/api/health")
        connection.putheader("Content-Length", str(MAX_DRAIN_BYTES + 1))
        connection.endheaders()
        response = connection.getresponse()
        status, content_type, _ = read(response)

        assert status == 405
        assert content_type == "application/json"
        assert response.will_close
