"""
Regression tests for src/classify.py, using real, previously-resolved
production incidents as ground truth. A change to classify.py that no
longer reaches the same real, human-confirmed category is a regression,
not an improvement — even if it "looks" more sophisticated.
"""
import json
from pathlib import Path

import pytest

from src.classify import classify_failure, Category
from src.detect import FailureSignal

FIXTURES_PATH = Path(__file__).parent / "fixtures" / "real_incidents.json"


def _load_fixtures():
    return json.loads(FIXTURES_PATH.read_text())


@pytest.mark.parametrize("incident", _load_fixtures(), ids=lambda i: i["id"])
def test_classification_matches_real_incident(incident):
    signal = FailureSignal(run_id="test", job_id="0", state_message=incident["error_text"])
    result = classify_failure(signal, incident["error_text"])
    assert result.category.value == incident["expected_category"], (
        f"{incident['id']}: expected {incident['expected_category']}, "
        f"got {result.category.value}. Reason given: {result.reason}"
    )


def test_no_new_data_alone_is_ambiguous_not_healthy():
    """
    A bare 'no new data available' message with no other structural
    signature must be flagged ambiguous, never treated as a clean/
    healthy signal — this is the exact pattern that masked a real
    upstream schema break in the psoobe-stream-prod incident.
    """
    signal = FailureSignal(run_id="test", job_id="0")
    result = classify_failure(signal, "No new psoobe stream data is available for processing")
    assert result.category == Category.AMBIGUOUS


def test_recurring_failure_rules_out_transient_over_time():
    """
    A single TIMED OUT is plausibly transient, but the real
    combined-app-instances-prod incident recurred across multiple runs
    — this should NOT be silently auto-retriggered forever without
    surfacing that it's a real, recurring pattern.
    """
    signal = FailureSignal(run_id="test", job_id="0")
    result = classify_failure(signal, "TIMED OUT")
    assert result.category == Category.TRANSIENT
    # The recurrence check itself is exercised in test_rca_hypothesis_logic.py
    # via _test_against_run_history, since it needs real run-history data.
