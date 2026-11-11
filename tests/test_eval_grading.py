"""The eval grader: findings are judged by replaying their tests, not by trusting the report."""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evals"))

import oracle  # noqa: E402
import run_eval  # noqa: E402


def grade(bugs, findings, report):
    case = {"id": "t", "merchant": "acme_utilities", "bugs": bugs}
    return asyncio.run(run_eval.grade_case(case, report, findings))


def test_right_finding_counts_and_is_attributed_to_its_bug():
    g, _, meta = grade(["B02", "B04", "B07"], [oracle.finding("B04")], "Verdict: NOT READY")
    assert meta["found"] == ["B04"] and meta["missed"] == ["B02", "B07"]
    assert g["recall"] == 1 / 3 and g["false_pos"] == 0 and g["case_pass"] == 0


def test_wrong_test_and_non_reproducing_test_are_false_positives():
    # BOGUS fails even on a clean API; the B01 test does not fail when only B08 is on.
    g, _, meta = grade(["B08"], [oracle.BOGUS, oracle.finding("B01")], "Verdict: NOT READY")
    assert g["false_pos"] == 2 and meta["found"] == []


def test_any_finding_on_a_clean_build_is_a_false_alarm():
    g, _, _ = grade([], [oracle.finding("B03")], "Verdict: READY")
    assert g["false_pos"] == 1 and g["case_pass"] == 0


def test_verdict_is_read_from_common_markdown_shapes():
    for text in ("Verdict: NOT READY", "**Verdict:** NOT READY", "# Verdict: Not Ready\nmore", "intro\n\nVerdict: NOT READY"):
        assert run_eval.read_verdict(text) == "NOT READY", text
    assert run_eval.read_verdict("Verdict: READY") == "READY"
    assert run_eval.read_verdict("The merchant looks ready.") is None
