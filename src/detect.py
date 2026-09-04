"""
Step 1-2 of pipeline-incident-remediation: fetch recent failures and
split any pasted/multi-signal incident text into distinct exception
blocks before classification.
"""
import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from config.connection import databricks_get, QueryFailedError

BASELINE_PATH = Path(
    os.environ.get(
        "PIPELINE_REMEDIATION_BASELINE_PATH",
        os.path.expanduser("~/pipeline-remediation-state/baseline.json"),
    )
)


@dataclass
class FailureSignal:
    run_id: str
    job_id: Optional[str] = None
    job_name: Optional[str] = None
    error_text: str = ""
    state_message: str = ""
    start_time: Optional[str] = None


def _load_baseline() -> str:
    if not BASELINE_PATH.exists():
        return (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    return json.loads(BASELINE_PATH.read_text()).get("last_run", "")


def _save_baseline() -> None:
    BASELINE_PATH.parent.mkdir(parents=True, exist_ok=True)
    BASELINE_PATH.write_text(
        json.dumps({"last_run": datetime.now(timezone.utc).isoformat()})
    )


def fetch_recent_failures(limit: int = 25) -> list[FailureSignal]:
    """
    Cheap, deterministic check — no LLM reasoning here. Queries the
    Jobs REST API directly (never WorkspaceClient / databricks-sql-
    connector, per config/connection.py's hard rules).
    """
    baseline = _load_baseline()

    try:
        result = databricks_get(
            "/api/2.1/jobs/runs/list",
            params={"active_only": "false", "limit": limit},
        )
    except QueryFailedError:
        # Fail loudly upward — never silently return an empty list that
        # could be misread as "no failures."
        raise

    failures = []
    for run in result.get("runs", []):
        state = run.get("state", {})
        if state.get("result_state") != "FAILED":
            continue
        start_time = run.get("start_time")
        if start_time and baseline:
            # start_time from the API is epoch ms; baseline is ISO.
            run_dt = datetime.fromtimestamp(start_time / 1000, tz=timezone.utc)
            baseline_dt = datetime.fromisoformat(baseline)
            if run_dt <= baseline_dt:
                continue

        failures.append(
            FailureSignal(
                run_id=str(run.get("run_id")),
                job_id=str(run.get("job_id")),
                job_name=run.get("run_name"),
                state_message=state.get("state_message", ""),
                start_time=str(start_time) if start_time else None,
            )
        )

    _save_baseline()
    return failures


def split_incident_text(raw_text: str) -> list[str]:
    """
    Real incident text often bundles multiple exceptions from different
    job runs into one paste (e.g. a schema error, followed by "no new
    data available" on the retry). Split into distinct blocks — do not
    let downstream classification treat this as one signal by default.

    This is a heuristic split on common exception-boundary markers; the
    correlation check for whether split signals are actually related
    happens later, in rca.py, using real evidence — not here.
    """
    boundary_pattern = re.compile(
        r"(?=\[\w+(?:_\w+)*\]|Exception:|Error:|Traceback)", re.MULTILINE
    )
    blocks = [b.strip() for b in boundary_pattern.split(raw_text) if b.strip()]
    return blocks if blocks else [raw_text.strip()]
