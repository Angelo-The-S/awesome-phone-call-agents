"""Unit and light integration tests for client.py's reused primitives:
the REST transport (CallEClient), task-hardening (build_hardened_task),
and disclosure-script rendering (render_disclosure_script) - all against
fake_server.py only, never api.heycall-e.com.

client.py has no CLI of its own; resolver.py is Reality Resolver's only
entry point (see tests/test_resolver_e2e.py for its own end-to-end
coverage). This file only proves the primitives resolver.py builds on.
"""

from __future__ import annotations

from typing import Any

import pytest

import client as client_module
from client import (
    CALL_CLOSING_INSTRUCTIONS,
    DISCLOSURE_INSTRUCTION_HEADER,
    NO_REPEAT_OPENING_INSTRUCTIONS,
    PROACTIVE_NEXT_STEP_INSTRUCTIONS,
    REAL_API_BASE_URL,
    TASK_INJECTION_RESISTANCE_INSTRUCTIONS,
    VOICEMAIL_HANDLING_INSTRUCTIONS,
    CallEAPIError,
    CallEClient,
    LiveCallBlockedError,
    build_hardened_task,
    build_recipient,
    render_disclosure_script,
)
from compliance.jurisdictions import fr, us_federal
from fake_server import INSUFFICIENT_BALANCE_PHONE, RATE_LIMITED_ONCE_PHONE, FakeCalleServer
from verdict import patient_intent_result_schema

TEST_API_KEY = "iams_live_fake_test_key_do_not_use"

FR_PHONE = "+33639980456"  # ARCEP Numbering Plan Art. 2.5.12 reserved mobile block "06 39 98"


def test_live_base_url_is_blocked_without_allow_live() -> None:
    with pytest.raises(LiveCallBlockedError):
        CallEClient(base_url=REAL_API_BASE_URL, api_key=TEST_API_KEY, allow_live=False)


def test_create_and_poll_reaches_completed_with_structured_result() -> None:
    """Proves the REST transport itself (CallEClient) works end to end
    against the fake server, using the same patient_intent_result_schema
    Reality Resolver actually sends.
    """
    with FakeCalleServer() as server:
        client = CallEClient(base_url=server.base_url, api_key=TEST_API_KEY)
        recipient = build_recipient(FR_PHONE, locale="fr-FR", region="FR")

        created = client.create_call(
            task="Call the recipient to confirm their appointment.",
            recipients=[recipient],
            result_schema=patient_intent_result_schema(),
            idempotency_key="test-happy-path-1",
        )
        assert created["status"] == "queued"
        assert created["id"].startswith("call_")

        final_call = client.poll_until_terminal(created["id"], interval_seconds=0.01, timeout_seconds=5)

        assert final_call["status"] == "completed"
        assert final_call["structured_result"] == {
            "patient_intent": "confirmed",
            "answered_by": "human",
            "confidence_note": "Fake server: deterministic canned result, not extracted from real call evidence.",
            "manipulation_attempt_detected": False,
        }
        assert final_call["recipients"][0]["locale"] == "fr-FR"
        assert final_call["recipients"][0]["region"] == "FR"
        assert server.creates == 1


class _FakeClock:
    """Lets poll_until_terminal tests simulate minutes of elapsed time
    instantly instead of actually sleeping - fake_server.py's CallRecord
    status is driven by read count, not wall-clock time, so it can't
    simulate a long-running call on its own; these tests monkeypatch
    client.time.monotonic/client.time.sleep and stub get_call directly.
    """

    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def _stub_get_call(statuses: list[str]) -> Any:
    calls = {"count": 0}

    def get_call(self, call_id: str) -> dict[str, Any]:
        index = min(calls["count"], len(statuses) - 1)
        calls["count"] += 1
        return {"id": call_id, "status": statuses[index]}

    return get_call


def test_poll_until_terminal_polls_indefinitely_by_default(monkeypatch) -> None:
    clock = _FakeClock()
    monkeypatch.setattr(client_module.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(client_module.time, "sleep", clock.sleep)
    monkeypatch.setattr(
        CallEClient, "get_call", _stub_get_call(["in_progress"] * 20 + ["completed"])
    )

    api_client = CallEClient(base_url="http://fake", api_key=TEST_API_KEY)
    final_call = api_client.poll_until_terminal("call_123", interval_seconds=60.0)

    assert final_call["status"] == "completed"


def test_poll_until_terminal_warns_repeatedly_at_expected_intervals(monkeypatch) -> None:
    clock = _FakeClock()
    monkeypatch.setattr(client_module.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(client_module.time, "sleep", clock.sleep)
    # 16 non-terminal reads then completed: crosses the 300s/600s/900s warn
    # thresholds (at reads 6, 11, 16) and reaches "completed" on read 17,
    # just before a 4th warning (1200s) would otherwise fire.
    monkeypatch.setattr(
        CallEClient, "get_call", _stub_get_call(["in_progress"] * 16 + ["completed"])
    )

    warnings: list[float] = []
    api_client = CallEClient(base_url="http://fake", api_key=TEST_API_KEY)
    api_client.poll_until_terminal(
        "call_123",
        interval_seconds=60.0,
        warn_after_seconds=300.0,
        on_warn=lambda minutes, call: warnings.append(minutes),
    )

    assert warnings == [5.0, 10.0, 15.0]


def test_poll_until_terminal_no_warnings_when_disabled(monkeypatch) -> None:
    clock = _FakeClock()
    monkeypatch.setattr(client_module.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(client_module.time, "sleep", clock.sleep)
    monkeypatch.setattr(
        CallEClient, "get_call", _stub_get_call(["in_progress"] * 30 + ["completed"])
    )

    warnings: list[float] = []
    api_client = CallEClient(base_url="http://fake", api_key=TEST_API_KEY)
    api_client.poll_until_terminal(
        "call_123",
        interval_seconds=60.0,
        warn_after_seconds=None,
        on_warn=lambda minutes, call: warnings.append(minutes),
    )

    assert warnings == []


def test_poll_until_terminal_explicit_timeout_still_raises(monkeypatch) -> None:
    clock = _FakeClock()
    monkeypatch.setattr(client_module.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(client_module.time, "sleep", clock.sleep)
    monkeypatch.setattr(CallEClient, "get_call", _stub_get_call(["in_progress"]))

    api_client = CallEClient(base_url="http://fake", api_key=TEST_API_KEY)
    with pytest.raises(TimeoutError):
        api_client.poll_until_terminal("call_123", interval_seconds=10.0, timeout_seconds=30.0)


def test_insufficient_balance_error_is_surfaced() -> None:
    with FakeCalleServer() as server:
        client = CallEClient(base_url=server.base_url, api_key=TEST_API_KEY)
        recipient = build_recipient(INSUFFICIENT_BALANCE_PHONE, locale="fr-FR", region="FR")

        with pytest.raises(CallEAPIError) as exc_info:
            client.create_call(task="Call the recipient.", recipients=[recipient])

        assert exc_info.value.code == "insufficient_balance"
        assert exc_info.value.status_code == 402


def test_unsupported_region_error_is_surfaced() -> None:
    with FakeCalleServer() as server:
        client = CallEClient(base_url=server.base_url, api_key=TEST_API_KEY)
        recipient = build_recipient(FR_PHONE, locale="fr-FR", region="ZZ")

        with pytest.raises(CallEAPIError) as exc_info:
            client.create_call(task="Call the recipient.", recipients=[recipient])

        assert exc_info.value.code == "unsupported_region"


def test_unsupported_language_error_is_surfaced() -> None:
    with FakeCalleServer() as server:
        client = CallEClient(base_url=server.base_url, api_key=TEST_API_KEY)
        recipient = build_recipient(FR_PHONE, locale="zz-ZZ", region="FR")

        with pytest.raises(CallEAPIError) as exc_info:
            client.create_call(task="Call the recipient.", recipients=[recipient])

        assert exc_info.value.code == "unsupported_language"


def test_invalid_phone_is_rejected_locally_before_any_request() -> None:
    with FakeCalleServer() as server:
        with pytest.raises(ValueError):
            build_recipient("not-a-phone", locale="fr-FR", region="FR")
        assert server.requests == 0


def test_unauthorized_when_api_key_is_empty() -> None:
    with FakeCalleServer() as server:
        client = CallEClient(base_url=server.base_url, api_key="")
        recipient = build_recipient(FR_PHONE, locale="fr-FR", region="FR")

        with pytest.raises(CallEAPIError) as exc_info:
            client.create_call(task="Call the recipient.", recipients=[recipient])

        assert exc_info.value.code == "unauthorized"
        assert exc_info.value.status_code == 401


def test_create_call_raises_immediately_on_rate_limit_without_retrying() -> None:
    """POST /v1/calls is never retried automatically, not even for a
    transient-looking status like 429 - see the module docstring and
    CallEClient._raise_ambiguous_post_failure. GET/poll_until_terminal
    keeps its own retry-with-backoff, unaffected by this.
    """
    with FakeCalleServer() as server:
        client = CallEClient(base_url=server.base_url, api_key=TEST_API_KEY)
        recipient = build_recipient(RATE_LIMITED_ONCE_PHONE, locale="fr-FR", region="FR")

        with pytest.raises(CallEAPIError) as exc_info:
            client.create_call(
                task="Call the recipient.",
                recipients=[recipient],
                idempotency_key="test-rate-limit-1",
            )
        assert exc_info.value.code == "rate_limit_exceeded"


def test_create_call_never_retries_ambiguous_failure_with_idempotency_key(monkeypatch) -> None:
    """A POST that gets no confirmed HTTP response (timeout, connection
    error) must never be retried automatically - even though CALL-E
    guarantees replaying the same Idempotency-Key and body would be safe,
    a call-creation request that might already have been accepted by the
    provider is never silently repeated. The failure surfaces immediately
    instead.
    """
    call_count = {"n": 0}

    def fake_urlopen(request: object, timeout: float | None = None) -> None:
        call_count["n"] += 1
        raise TimeoutError("The read operation timed out")

    monkeypatch.setattr(client_module.urllib.request, "urlopen", fake_urlopen)

    api_client = CallEClient(base_url="http://fake", api_key=TEST_API_KEY)
    with pytest.raises(RuntimeError) as exc_info:
        api_client.create_call(task="Call the recipient.", idempotency_key="idem-no-retry-test")

    assert call_count["n"] == 1
    message = str(exc_info.value)
    assert "no automatic retry" in message
    assert "Idempotency-Key was idem-no-retry-test" in message


def test_create_call_never_retries_ambiguous_failure_without_idempotency_key(monkeypatch) -> None:
    call_count = {"n": 0}

    def fake_urlopen(request: object, timeout: float | None = None) -> None:
        call_count["n"] += 1
        raise TimeoutError("The read operation timed out")

    monkeypatch.setattr(client_module.urllib.request, "urlopen", fake_urlopen)

    api_client = CallEClient(base_url="http://fake", api_key=TEST_API_KEY)
    with pytest.raises(RuntimeError) as exc_info:
        api_client.create_call(task="Call the recipient.")

    assert call_count["n"] == 1
    assert "Idempotency-Key was <none>" in str(exc_info.value)


def test_build_hardened_task_is_operator_task_plus_fixed_blocks_in_order() -> None:
    operator_task = "Call the recipient to confirm their appointment."
    expected = (
        f"{operator_task}\n\n{TASK_INJECTION_RESISTANCE_INSTRUCTIONS}"
        f"\n\n{VOICEMAIL_HANDLING_INSTRUCTIONS}"
        f"\n\n{NO_REPEAT_OPENING_INSTRUCTIONS}"
        f"\n\n{PROACTIVE_NEXT_STEP_INSTRUCTIONS}"
        f"\n\n{CALL_CLOSING_INSTRUCTIONS}"
    )
    assert build_hardened_task(operator_task) == expected


def test_build_hardened_task_disclosure_script_comes_first() -> None:
    """AI disclosure must happen at the very start of the call - the
    disclosure block comes before the operator's own task, before
    everything else.
    """
    operator_task = "Call the recipient to confirm their appointment."
    disclosure_script = "This call is made by an artificial intelligence system."
    result = build_hardened_task(operator_task, disclosure_script)

    disclosure_index = result.index(DISCLOSURE_INSTRUCTION_HEADER)
    task_index = result.index(operator_task)
    resistance_index = result.index(TASK_INJECTION_RESISTANCE_INSTRUCTIONS)
    voicemail_index = result.index(VOICEMAIL_HANDLING_INSTRUCTIONS)
    no_repeat_index = result.index(NO_REPEAT_OPENING_INSTRUCTIONS)
    proactive_index = result.index(PROACTIVE_NEXT_STEP_INSTRUCTIONS)
    closing_index = result.index(CALL_CLOSING_INSTRUCTIONS)
    assert (
        disclosure_index
        < task_index
        < resistance_index
        < voicemail_index
        < no_repeat_index
        < proactive_index
        < closing_index
    )
    assert disclosure_script in result


def test_build_hardened_task_call_closing_comes_last() -> None:
    operator_task = "Call the recipient to confirm their appointment."
    result = build_hardened_task(operator_task)

    assert CALL_CLOSING_INSTRUCTIONS in result
    voicemail_index = result.index(VOICEMAIL_HANDLING_INSTRUCTIONS)
    closing_index = result.index(CALL_CLOSING_INSTRUCTIONS)
    assert voicemail_index < closing_index


def test_render_disclosure_script_fills_all_placeholder_kinds() -> None:
    result = render_disclosure_script(us_federal.DISCLOSURE_SCRIPT, "Bright Smile Dental", "Alex")
    assert "[AGENT_NAME]" not in result
    assert "[ENTITY]" not in result
    assert "[REASON_FOR_CALLING]" not in result
    assert "[CALLBACK_NUMBER]" not in result
    assert "Bright Smile Dental" in result
    assert "Alex" in result


def test_render_disclosure_script_generic_fallback_without_entity_name() -> None:
    result = render_disclosure_script(us_federal.DISCLOSURE_SCRIPT, None, None)
    assert "[ENTITY]" not in result
    assert "this organization" in result


def test_render_disclosure_script_french_fallback() -> None:
    result = render_disclosure_script(fr.DISCLOSURE_SCRIPT, None, None)
    assert "[ENTITE]" not in result
    assert "cette organisation" in result


def test_render_disclosure_script_agent_name_fallback_is_neutral() -> None:
    result = render_disclosure_script(us_federal.DISCLOSURE_SCRIPT, None, None)
    assert "[AGENT_NAME]" not in result
    assert "an automated calling agent" in result


def test_render_disclosure_script_reason_instruction_forbids_asking_recipient() -> None:
    result = render_disclosure_script(us_federal.DISCLOSURE_SCRIPT, None, None)
    assert "[REASON_FOR_CALLING]" not in result
    assert "do not ask the recipient" in result


def test_render_disclosure_script_reason_comes_before_closing_statement() -> None:
    en_result = render_disclosure_script(us_federal.DISCLOSURE_SCRIPT, None, None)
    reason_index = en_result.index("state briefly and naturally why you are calling")
    closing_index = en_result.index("This call uses an artificial voice")
    assert reason_index < closing_index

    fr_result = render_disclosure_script(fr.DISCLOSURE_SCRIPT, None, None)
    reason_index_fr = fr_result.index("expliquez brievement")
    closing_index_fr = fr_result.index("Vous pouvez demander")
    assert reason_index_fr < closing_index_fr
