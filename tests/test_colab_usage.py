"""Parsing of `colab usage` / `colab sessions` output (contract 2.3)."""

import re

from lms_kernel import colab_usage

USAGE = "Current balance: 576.13 compute units\nUsage rate: 1.15/hr\nActive assignments: 1\n"
SESSIONS = (
    "[lms] gpu-t4-s-ass1c1-abc123 | Hardware: T4 | Shape: Standard | Variant: GPU\n"
    "[?] gpu-a100-s-usw1-def456 | Hardware: A100 | Shape: High-RAM | Variant: GPU\n"
    "[cpu] cpu-s-xyz | Hardware: CPU | Shape: Standard | Variant: DEFAULT | Status: IDLE\n"
)


def test_parses_measured_cli_0_7_5_output():
    body = colab_usage.build(USAGE, SESSIONS, "lms", checked_at="2026-11-06T00:00:00Z")
    assert body == {
        "version": 1,
        "checked_at": "2026-11-06T00:00:00Z",
        "balance": 576.13,
        "rate_per_hour": 1.15,
        "sessions": [
            {"name": "lms", "machine": "T4", "current": True},
            {"name": None, "machine": "A100", "current": False},
            {"name": "cpu", "machine": "CPU", "current": False},
        ],
        "raw": None,
    }


def test_no_sessions():
    body = colab_usage.build(
        "Current balance: 576.13 compute units\nUsage rate: 0.00/hr\nActive assignments: 0\n",
        "[colab] No active sessions found on server.\n",
        "lms",
    )
    assert body["sessions"] == [] and body["rate_per_hour"] == 0.0 and body["raw"] is None


def test_unknown_format_gives_null_and_raw():
    body = colab_usage.build("Balance 12 CU, rate 3 per hour\n", "lms (T4)\n", "lms")
    assert body["balance"] is None and body["rate_per_hour"] is None and body["sessions"] is None
    assert body["raw"] == {"usage": "Balance 12 CU, rate 3 per hour", "sessions": "lms (T4)"}


def test_only_the_unparsed_command_goes_in_raw():
    body = colab_usage.build(USAGE, SESSIONS + "something new\n", "lms")
    assert body["balance"] == 576.13 and body["rate_per_hour"] == 1.15
    assert body["sessions"] is None
    assert body["raw"]["usage"] is None and body["raw"]["sessions"].endswith("something new")


def test_checked_at_is_utc_iso():
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", colab_usage.build(USAGE, SESSIONS, "x")["checked_at"])


def test_raw_is_scrubbed_and_capped():
    secret = "ya29.a0AfB_byC" + "x" * 50
    body = colab_usage.build("odd " + secret + "\n" + "y z " * 3000, "", "lms")
    raw = body["raw"]["usage"]
    assert secret not in raw and "<redacted>" in raw
    assert len(raw) == colab_usage.RAW_LIMIT
    assert body["raw"]["sessions"] == ""
