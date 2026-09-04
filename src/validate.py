"""
Step 8 of pipeline-incident-remediation: post-merge validation. A
merged PR is not proof of resolution — this module checks the job now
completes, the specific error signature does not recur, and applies
basic freshness/row-count sanity on the affected table. A partial or
recurred result reopens the incident rather than treating a clean merge
as closure.
"""
from dataclasses import dataclass, field
from enum import Enum

from config.connection import databricks_get, run_sql_statement, QueryFailedError


class RecoveryStatus(str, Enum):
    VERIFIED = "verified"
    PARTIAL = "partial"
    RECURRED = "recurred"


@dataclass
class ValidationResult:
    recovery_status: RecoveryStatus
    checks: list[str] = field(default_factory=list)
    remaining_impact: list[str] = field(default_factory=list)


def validate_fix(
    job_id: str,
    original_error_signature: str,
    affected_table: str | None = None,
) -> ValidationResult:
    checks = []
    all_passed = True

    # Check 1: the job now completes successfully.
    try:
        history = databricks_get(
            "/api/2.1/jobs/runs/list", params={"job_id": job_id, "limit": 1}
        )
        runs = history.get("runs", [])
        latest_succeeded = bool(runs) and runs[0].get("state", {}).get("result_state") == "SUCCESS"
        checks.append(f"Latest run succeeded: {'PASS' if latest_succeeded else 'FAIL'}")
        all_passed &= latest_succeeded
    except QueryFailedError as e:
        checks.append(f"Latest run check: FAIL ({e})")
        all_passed = False

    # Check 2: the specific error signature does not recur.
    try:
        history = databricks_get(
            "/api/2.1/jobs/runs/list", params={"job_id": job_id, "limit": 5}
        )
        recurred = any(
            original_error_signature.lower()
            in r.get("state", {}).get("state_message", "").lower()
            for r in history.get("runs", [])
        )
        checks.append(f"Error signature does not recur: {'PASS' if not recurred else 'FAIL'}")
        all_passed &= not recurred
    except QueryFailedError as e:
        checks.append(f"Error recurrence check: FAIL ({e})")
        all_passed = False

    # Check 3: freshness/row-count sanity, if a table was affected.
    if affected_table:
        try:
            rows = run_sql_statement(f"SELECT COUNT(*) FROM {affected_table}")
            row_count = int(rows[0][0]) if rows else 0
            checks.append(f"Affected table has rows ({row_count}): {'PASS' if row_count > 0 else 'FAIL'}")
            all_passed &= row_count > 0
        except QueryFailedError as e:
            checks.append(f"Table freshness check: FAIL ({e})")
            all_passed = False

    if all_passed:
        return ValidationResult(recovery_status=RecoveryStatus.VERIFIED, checks=checks)

    # Distinguish partial (some checks passed) from fully recurred.
    any_passed = any("PASS" in c for c in checks)
    status = RecoveryStatus.PARTIAL if any_passed else RecoveryStatus.RECURRED
    return ValidationResult(
        recovery_status=status,
        checks=checks,
        remaining_impact=["Incident reopened — see failed checks above."],
    )
