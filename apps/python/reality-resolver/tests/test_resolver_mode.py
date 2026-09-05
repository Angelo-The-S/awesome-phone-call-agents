"""Tests for the --mode demo|live separation in resolver.py.

demo (default): the compliance gate is always evaluated and displayed
honestly, but a failing result never stops the call - safe for local
testing and for judges cloning this repo at any hour, since
--allow-live requires --mode live, so no real call is ever reachable
while in demo mode. live: the compliance gate is fully enforced,
fail-closed, identical to the original compliance-gated-callback
behavior.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from fake_server import FakeCalleServer

HERE = Path(__file__).resolve().parent.parent
CASE = str(HERE / "cases" / "ghost-appointment.json")

# Both within 24h of the case's 2026-09-11T14:00:00Z deadline (threshold
# 24h), so R1-R4 all trigger identically for both - only the compliance
# gate's calling-window check differs between the two "now" values.
LEGAL_HOUR_NOW = "2026-09-10T20:00:00Z"  # 16:00 New York local (EDT) - within 8:00-21:00
ILLEGAL_HOUR_NOW = "2026-09-11T02:00:00Z"  # 22:00 New York local (EDT, previous day) - outside 8:00-21:00

US_COMPLIANT_FLAGS = [
    "--consent-obtained",
    "--consent-timestamp",
    "2026-08-20T12:00:00Z",
    "--dnc-checked",
    "--recipient-timezone",
    "America/New_York",
]


def _run_resolver(server_base_url: str, extra_args: list[str]) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env.pop("CALLE_API_KEY", None)
    return subprocess.run(
        [
            sys.executable,
            str(HERE / "resolver.py"),
            CASE,
            "--base-url",
            server_base_url,
            "--poll-interval-seconds",
            "0.01",
            *extra_args,
        ],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )


def test_demo_mode_illegal_hour_reaches_calle_but_flags_would_block_in_live() -> None:
    with FakeCalleServer() as server:
        result = _run_resolver(
            server.base_url,
            ["--mode", "demo", "--now-utc", ILLEGAL_HOUR_NOW, "--recipient-timezone", "America/New_York", "--execute"],
        )
        assert result.returncode == 0, result.stderr
        assert "MODE: DEMO" in result.stdout
        assert "would_block_in_live: True" in result.stdout
        assert "*** DEMO MODE: this call would be BLOCKED in live mode ***" in result.stdout
        assert "UNRESOLVED_CALL_BLOCKED" not in result.stdout
        assert "=== CALL-E ===" in result.stdout
        assert server.creates == 1
        assert "mode=demo, would_block_in_live=True" in result.stdout


def test_live_mode_illegal_hour_blocks_the_call() -> None:
    with FakeCalleServer() as server:
        result = _run_resolver(
            server.base_url,
            ["--mode", "live", "--now-utc", ILLEGAL_HOUR_NOW, "--recipient-timezone", "America/New_York", "--execute"],
        )
        assert result.returncode == 0, result.stderr
        assert "MODE: LIVE" in result.stdout
        assert "would_block_in_live: True" in result.stdout
        assert "Status: UNRESOLVED_CALL_BLOCKED" in result.stdout
        assert "Action: RETRY_WHEN_PERMITTED" in result.stdout
        assert "=== CALL-E ===" not in result.stdout
        assert server.creates == 0


def test_demo_mode_legal_hour_reaches_calle() -> None:
    with FakeCalleServer() as server:
        result = _run_resolver(
            server.base_url,
            ["--mode", "demo", "--now-utc", LEGAL_HOUR_NOW, *US_COMPLIANT_FLAGS, "--execute"],
        )
        assert result.returncode == 0, result.stderr
        assert "would_block_in_live: False" in result.stdout
        assert "DEMO MODE: this call would be BLOCKED" not in result.stdout
        assert "Status: RESOLVED" in result.stdout
        assert server.creates == 1


def test_live_mode_legal_hour_reaches_calle() -> None:
    with FakeCalleServer() as server:
        result = _run_resolver(
            server.base_url,
            ["--mode", "live", "--now-utc", LEGAL_HOUR_NOW, *US_COMPLIANT_FLAGS, "--execute"],
        )
        assert result.returncode == 0, result.stderr
        assert "would_block_in_live: False" in result.stdout
        assert "Status: RESOLVED" in result.stdout
        assert "Action: KEEP_SLOT" in result.stdout
        assert server.creates == 1


def test_demo_mode_still_shows_every_compliance_check_result() -> None:
    """Demo mode must never hide a failing check - only its enforcement
    changes, never its visibility.
    """
    with FakeCalleServer() as server:
        result = _run_resolver(
            server.base_url,
            ["--mode", "demo", "--now-utc", LEGAL_HOUR_NOW, "--recipient-timezone", "America/New_York", "--execute"],
        )
        assert result.returncode == 0, result.stderr
        assert "[FAIL] us_federal_consent" in result.stdout
        assert "[FAIL] us_federal_dnc_scrub" in result.stdout
        assert "[PASS] us_federal_calling_window" in result.stdout
        assert "would_block_in_live: True" in result.stdout


def test_dry_run_never_calls_regardless_of_mode() -> None:
    with FakeCalleServer() as server:
        result = _run_resolver(
            server.base_url,
            ["--mode", "demo", "--now-utc", ILLEGAL_HOUR_NOW, "--recipient-timezone", "America/New_York"],
        )
        assert result.returncode == 0, result.stderr
        assert "Created call" not in result.stdout
        assert server.creates == 0


def test_allow_live_without_mode_live_is_refused() -> None:
    with FakeCalleServer() as server:
        result = _run_resolver(server.base_url, ["--allow-live", "--now-utc", LEGAL_HOUR_NOW, *US_COMPLIANT_FLAGS])
        assert result.returncode == 1
        assert "requires --mode live" in result.stderr
        assert "Case:" not in result.stdout  # refused before even loading the case
        assert server.creates == 0


def test_now_utc_with_allow_live_is_refused() -> None:
    with FakeCalleServer() as server:
        result = _run_resolver(
            server.base_url, ["--mode", "live", "--allow-live", "--now-utc", LEGAL_HOUR_NOW, *US_COMPLIANT_FLAGS]
        )
        assert result.returncode == 1
        assert "cannot be combined with --allow-live" in result.stderr
        assert server.creates == 0
