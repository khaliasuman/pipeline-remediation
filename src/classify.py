"""
Step 2 of pipeline-incident-remediation: classify a failure signal as
transient, structural, or ambiguous, and auto-retrigger transient
failures (Step 3), deterministically — no LLM judgment in the retrigger
call itself.
"""
import re
from dataclasses import dataclass
from enum import Enum

from config.connection import databricks_post, QueryFailedError
from src.detect import FailureSignal


class Category(str, Enum):
    TRANSIENT = "transient"
    STRUCTURAL = "structural"
    AMBIGUOUS = "ambiguous"


@dataclass
class Classification:
    category: Category
    reason: str
    retriggered: bool = False
    retrigger_succeeded: bool = False


# Structural signatures — never auto-remediate, always go to RCA.
_STRUCTURAL_PATTERNS = [
    r"FIELD_NOT_FOUND",
    r"SCHEMA_MISMATCH",
    r"CANNOT_RESOLVE_COLUMN",
    r"NUM_COLUMNS_MISMATCH",
    r"DELTA_FAILED_TO_MERGE_FIELDS",
    r"PARQUET_TYPE_ILLEGAL",
    r"UNABLE_TO_INFER_SCHEMA",
    r"CAST_INVALID_INPUT",
    r"DELTA_INVALID_FORMAT",
    r"DELTA_PATH_BASED_ACCESS_TO_TABLE_BLOCKED",
    r"INSUFFICIENT_PERMISSIONS",
    r"DELTA_WRITE_INTO_VIEW_NOT_SUPPORTED",
    r"CHECKPOINT_RDD_BLOCK_ID_NOT_FOUND",
    r"SQLSTATE:\s*42\d{3}",  # any 42xxx SQLSTATE
    r"KeyError",
    r"AttributeError",
]

# Transient signatures — safe to auto-retrigger.
_TRANSIENT_PATTERNS = [
    r"SPARK_ERROR",
    r"SPARK_INTERNAL_ERROR",
    r"driver state change",
    r"TIMED?\s*OUT",
    r"FetchFailedException",
    r"Cluster.*terminated",
    r"Unexpected error occurred during Spark startup",
]

_NO_NEW_DATA_PATTERN = re.compile(r"no new .* data.*available", re.IGNORECASE)


def classify_failure(signal: FailureSignal, error_text: str = "") -> Classification:
    """
    Classifies a single failure signal. `error_text` is the raw error
    text for this specific signal (from detect.split_incident_text, or
    the signal's own state_message).
    """
    text = error_text or signal.state_message

    is_structural = any(re.search(p, text, re.IGNORECASE) for p in _STRUCTURAL_PATTERNS)
    is_transient = any(re.search(p, text, re.IGNORECASE) for p in _TRANSIENT_PATTERNS)

    # A "no new data available" message immediately following a
    # structural error on the same source is very likely a stalled
    # checkpoint — flag it explicitly, never treat it as clean/healthy.
    if _NO_NEW_DATA_PATTERN.search(text) and not is_structural and not is_transient:
        return Classification(
            category=Category.AMBIGUOUS,
            reason=(
                "'No new data available' with no other error signature — "
                "likely a downstream effect of an earlier structural "
                "failure, not independently classifiable. Treat as "
                "ambiguous, not healthy."
            ),
        )

    if is_structural:
        # Ties go to structural over transient — the cost of an
        # unnecessary RCA cycle is far lower than the cost of silent
        # data loss from a wrongly-auto-remediated structural failure.
        return Classification(
            category=Category.STRUCTURAL,
            reason=f"Matched structural error pattern in: {text[:200]}",
        )

    if is_transient:
        retriggered, succeeded = _retrigger(signal)
        return Classification(
            category=Category.TRANSIENT,
            reason=f"Matched transient error pattern in: {text[:200]}",
            retriggered=retriggered,
            retrigger_succeeded=succeeded,
        )

    return Classification(
        category=Category.AMBIGUOUS,
        reason=(
            f"No known structural or transient pattern matched: {text[:200]}. "
            "Defaulting to ambiguous — confidence: low downstream, not a "
            "forced transient/structural call."
        ),
    )


def _retrigger(signal: FailureSignal) -> tuple[bool, bool]:
    """
    Deterministic retrigger via the Jobs API — no LLM judgment here.
    Backfill is never attempted by this function or anywhere in this
    codebase; see SKILL.md's "Backfill — planned but not executed"
    section.
    """
    if not signal.job_id:
        return False, False
    try:
        databricks_post("/api/2.1/jobs/run-now", {"job_id": int(signal.job_id)})
        return True, True
    except QueryFailedError:
        return True, False
