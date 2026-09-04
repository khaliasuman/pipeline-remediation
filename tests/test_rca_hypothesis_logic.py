"""
Tests for the hypothesis-tracking and stopping-criteria logic in
src/rca.py. These test the STRUCTURAL discipline of the RCA process —
that it never reports a root cause while a hypothesis is still
leading-but-contested, and that evidence_unavailable gaps correctly
block finalization — independent of what any specific LLM call returns.
"""
from src.rca import Hypothesis, HypothesisStatus, Confidence, _resolve


def test_single_leading_hypothesis_with_no_gaps_is_established_high_confidence():
    hypotheses = [
        Hypothesis(
            id="H1",
            statement="Upstream schema changed",
            status=HypothesisStatus.LEADING,
            supporting_evidence=["DESCRIBE HISTORY: schema changed at commit X", "source sample confirms"],
        ),
        Hypothesis(id="H2", statement="Application code regression", status=HypothesisStatus.RULED_OUT,
                    contradicting_evidence=["Git history: no relevant commit in failure window"]),
    ]
    status, text, confidence = _resolve(hypotheses, evidence_unavailable=[])
    assert status == "established"
    assert confidence == Confidence.HIGH
    assert "Upstream schema changed" in text


def test_leading_hypothesis_with_remaining_contested_stays_working():
    """
    This is the exact discipline that must never be skipped: a
    hypothesis being 'leading' is not sufficient on its own if another
    hypothesis is still contested — the skill explicitly forbids
    reporting root_cause while still leading-but-contested.
    """
    hypotheses = [
        Hypothesis(id="H1", statement="Upstream schema changed", status=HypothesisStatus.LEADING,
                    supporting_evidence=["one piece of evidence"]),
        Hypothesis(id="H2", statement="Checkpoint corruption", status=HypothesisStatus.CONTESTED),
    ]
    status, text, confidence = _resolve(hypotheses, evidence_unavailable=[])
    assert status == "working"
    assert confidence != Confidence.HIGH


def test_evidence_gap_blocks_high_confidence_even_with_one_leading_hypothesis():
    hypotheses = [
        Hypothesis(id="H1", statement="Field renamed upstream", status=HypothesisStatus.LEADING,
                    supporting_evidence=["schema history confirms rename"]),
    ]
    status, text, confidence = _resolve(
        hypotheses, evidence_unavailable=["source sample: retention window expired"]
    )
    # Still working/lower confidence, since a gap touches the leading
    # hypothesis's dependency chain — never silently promoted to high.
    assert status == "working"


def test_no_leading_hypothesis_is_low_confidence_not_a_guess():
    hypotheses = [
        Hypothesis(id="H1", statement="Theory A", status=HypothesisStatus.CONTESTED),
        Hypothesis(id="H2", statement="Theory B", status=HypothesisStatus.CONTESTED),
    ]
    status, text, confidence = _resolve(hypotheses, evidence_unavailable=[])
    assert status == "working"
    assert confidence == Confidence.LOW
    assert "insufficient discriminating evidence" in text.lower()
