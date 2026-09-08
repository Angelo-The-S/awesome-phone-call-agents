"""Internal objects -> safe HTTP representations.

One rule, and it is the reason this module exists as its own file rather
than as dict literals inside the request handler: no internal object is
ever handed to json.dumps directly. Every field a client receives is
named here, explicitly, one at a time.

That rules out dataclasses.asdict() and repr() on engine objects. Both
are convenient and both are exactly wrong for this job - they serialize
whatever the object happens to hold today, so a field added upstream
later would start reaching clients without anyone deciding it should.
A Case, for instance, carries call_phone in the clear; it is masked here
and only here.

Phase 2 needs health, case metadata and errors. Later phases add
resolution payloads on the same rule.
"""

from __future__ import annotations

from typing import Any

from client import mask_phone
from evidence.model import Case, Evidence

# Case files are authored server-side, not by any client, so this is not
# an injection boundary - it is a bound, so that one oversized claim in a
# case file cannot turn into an unbounded HTTP response. json.dumps
# already escapes control characters, which is the other half of the
# problem the CLI's sanitize_for_display() solves for a terminal.
MAX_TEXT_CHARS = 2000

TRUNCATION_MARKER = "...[truncated]"


def _text(value: str, max_chars: int = MAX_TEXT_CHARS) -> str:
    if len(value) <= max_chars:
        return value
    return value[: max(0, max_chars - len(TRUNCATION_MARKER))] + TRUNCATION_MARKER


def _hours(seconds: float) -> float | int:
    hours = seconds / 3600.0
    return int(hours) if hours.is_integer() else hours


def health_payload(mode: str, engine_version: str) -> dict[str, Any]:
    """Deliberately three flat fields. No base URL, no configuration
    dump, no build path, no environment echo - a health endpoint is the
    classic place those leak from.
    """
    return {"status": "ok", "mode": mode, "engine_version": engine_version}


def error_payload(code: str, message: str) -> dict[str, Any]:
    """The single error shape. `message` must be text this code wrote,
    never str(exc) from an unexpected failure and never a traceback.
    """
    return {"error": {"code": code, "message": message}}


def evidence_item(item: Evidence) -> dict[str, Any]:
    return {
        "source": _text(item.source),
        "type": item.type.value,
        "freshness_hours": _hours(item.freshness.total_seconds()),
        "claim": _text(item.claim),
        "ambiguity": item.ambiguity.value,
    }


def case_metadata(case: Case) -> dict[str, Any]:
    """Everything a client needs to display a case and choose one, and
    nothing else.

    Two omissions are deliberate. call_phone appears only masked, under a
    name that says so, so no client-side mistake can dial it or log it.
    call_task_hint is left out entirely: it is the operator instruction
    that seeds the task actually spoken on a call, and a client that has
    never needed it should not be handed it by default.
    """
    return {
        "name": _text(case.name),
        "use_case": _text(case.use_case),
        "deadline": case.deadline.isoformat().replace("+00:00", "Z"),
        "decision_deadline_threshold_hours": _hours(case.decision_deadline_threshold.total_seconds()),
        "decision_options": {str(k): _text(str(v)) for k, v in case.decision_options.items()},
        "evidence": [evidence_item(item) for item in case.evidence.items],
        "call_phone_masked": mask_phone(case.call_phone),
    }


def cases_payload(cases: tuple[Case, ...]) -> dict[str, Any]:
    return {"cases": [case_metadata(case) for case in cases]}
