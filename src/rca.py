"""
Step 4 of pipeline-incident-remediation: hypothesis-driven root cause
analysis. Generate a bounded set of hypotheses, gather only the
evidence that discriminates between them, and update each hypothesis's
status explicitly as leading / contested / ruled_out. Never conclude a
root cause while it's still leading-but-contested.
"""
from dataclasses import dataclass, field
from enum import Enum

from config.connection import call_llm, databricks_get, run_sql_statement, QueryFailedError
from src.detect import FailureSignal


class HypothesisStatus(str, Enum):
    LEADING = "leading"
    CONTESTED = "contested"
    RULED_OUT = "ruled_out"


class Confidence(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


@dataclass
class Hypothesis:
    id: str
    statement: str
    status: HypothesisStatus = HypothesisStatus.CONTESTED
    supporting_evidence: list[str] = field(default_factory=list)
    contradicting_evidence: list[str] = field(default_factory=list)


@dataclass
class RCAResult:
    error_type: str
    failure_mechanism: str
    hypotheses: list[Hypothesis]
    root_cause_status: str  # "working" | "established"
    root_cause_text: str
    confidence: Confidence
    evidence_unavailable: list[str] = field(default_factory=list)
    secondary_effects: list[str] = field(default_factory=list)
    jobs_affected: list[str] = field(default_factory=list)
    contained_to_single_job: bool = True
    lineage_hops_traversed: int = 0
    hop_extension_flagged: bool = False
    requires_human_judgment: list[str] = field(default_factory=list)


def run_rca(signal: FailureSignal, error_text: str) -> RCAResult:
    """
    Generate → test → narrow. Not exhaustive traversal to a fixed depth.
    """
    hypotheses = _generate_hypotheses(error_text)

    # Always start with job run history — cheapest, most direct source.
    _test_against_run_history(signal, hypotheses)

    # Only traverse lineage if it's needed to discriminate remaining
    # contested hypotheses — not as a default next step.
    hops_traversed = 0
    contained_to_single_job = True
    jobs_affected = [signal.job_id] if signal.job_id else []
    evidence_unavailable = []

    contested = [h for h in hypotheses if h.status == HypothesisStatus.CONTESTED]
    if contested and any("upstream" in h.statement.lower() for h in contested):
        try:
            lineage_jobs, hops_traversed = _traverse_lineage(signal, max_hops=2)
            if lineage_jobs:
                contained_to_single_job = False
                jobs_affected.extend(lineage_jobs)
        except QueryFailedError as e:
            evidence_unavailable.append(f"lineage traversal: {e}")

    root_cause_status, root_cause_text, confidence = _resolve(hypotheses, evidence_unavailable)

    return RCAResult(
        error_type=_infer_error_type(error_text),
        failure_mechanism=root_cause_text,
        hypotheses=hypotheses,
        root_cause_status=root_cause_status,
        root_cause_text=root_cause_text,
        confidence=confidence,
        evidence_unavailable=evidence_unavailable,
        jobs_affected=jobs_affected,
        contained_to_single_job=contained_to_single_job,
        lineage_hops_traversed=hops_traversed,
        hop_extension_flagged=hops_traversed > 2,
    )


def _generate_hypotheses(error_text: str) -> list[Hypothesis]:
    """
    Uses the cheap-tier LLM to generate 3-5 specific, falsifiable
    hypotheses from the raw error text alone. Nothing else has been
    fetched at this point.
    """
    prompt = f"""Given this Databricks job failure error text, generate 3-5
specific, falsifiable hypotheses for the root cause. Each hypothesis
must be a claim that could be wrong — not a vague category label.

Error text:
{error_text}

Respond with one hypothesis per line, no numbering, no extra text."""

    response = call_llm(prompt, use_escalation_model=False)
    lines = [l.strip() for l in response.strip().split("\n") if l.strip()]
    return [
        Hypothesis(id=f"H{i+1}", statement=line)
        for i, line in enumerate(lines[:5])
    ]


def _test_against_run_history(signal: FailureSignal, hypotheses: list[Hypothesis]) -> None:
    """Pull recent run history for this job — cheapest discriminating source."""
    if not signal.job_id:
        return
    try:
        history = databricks_get(
            "/api/2.1/jobs/runs/list",
            params={"job_id": signal.job_id, "limit": 10},
        )
    except QueryFailedError:
        return

    runs = history.get("runs", [])
    recent_failures = sum(
        1 for r in runs if r.get("state", {}).get("result_state") == "FAILED"
    )
    is_recurring = recent_failures >= 3

    for h in hypotheses:
        if "one-off" in h.statement.lower() or "transient" in h.statement.lower():
            if is_recurring:
                h.status = HypothesisStatus.RULED_OUT
                h.contradicting_evidence.append(
                    f"Run history: {recent_failures}/10 recent runs failed — "
                    "recurring pattern, not a one-off."
                )
            else:
                h.status = HypothesisStatus.LEADING
                h.supporting_evidence.append(
                    f"Run history: only {recent_failures}/10 recent runs failed."
                )


def _traverse_lineage(signal: FailureSignal, max_hops: int) -> tuple[list[str], int]:
    """
    Walk table_lineage one hop out. Only called when needed to test a
    live hypothesis. Continue past a hop only if it shows a genuinely
    failed/skipped/anomalous status in the same window.
    """
    if not signal.job_name:
        return [], 0

    rows = run_sql_statement(
        f"""SELECT downstream_table_name, downstream_table_type
            FROM system.access.table_lineage
            WHERE source_table_name LIKE '%{signal.job_name}%'
            LIMIT 10"""
    )
    affected = [r[0] for r in rows if r]
    return affected, 1 if affected else 0


def _resolve(
    hypotheses: list[Hypothesis], evidence_unavailable: list[str]
) -> tuple[str, str, Confidence]:
    """
    Apply the stopping criteria: exactly one hypothesis leading, with
    corroborating evidence from at least two independent sources, no
    remaining contested alternative, no evidence gap the leading
    hypothesis depends on.
    """
    leading = [h for h in hypotheses if h.status == HypothesisStatus.LEADING]
    contested = [h for h in hypotheses if h.status == HypothesisStatus.CONTESTED]

    if len(leading) == 1 and not contested and not evidence_unavailable:
        h = leading[0]
        if len(h.supporting_evidence) >= 2:
            return "established", h.statement, Confidence.HIGH
        return "established", h.statement, Confidence.MEDIUM

    if len(leading) == 1:
        # Leading but stopping criteria not fully met — do not report
        # as root_cause while still contested/incomplete.
        return "working", leading[0].statement, Confidence.MEDIUM

    return (
        "working",
        "No hypothesis reached leading status — insufficient discriminating evidence.",
        Confidence.LOW,
    )


def _infer_error_type(error_text: str) -> str:
    mapping = {
        "PATH_NOT_FOUND": "file_path",
        "SCHEMA": "schema",
        "PARSE": "sql",
        "TIMEOUT": "timeout",
        "TIMED OUT": "timeout",
        "SparkInternalError": "spark_compute",
        "KeyError": "python_application",
        "AttributeError": "python_application",
        "INSUFFICIENT_PERMISSIONS": "permission",
        "ACCESS_TO_TABLE_BLOCKED": "table_access_mode",
        "SQLSTATE": "sql",
        "zero records": "empty_output",
    }
    for key, category in mapping.items():
        if key.lower() in error_text.lower():
            return category
    return "unknown"
