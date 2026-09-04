"""
Persists the full RCA conversation and tool-call history to a Delta
table — so every investigation is queryable and auditable after the
fact, not just visible in the notebook output while it runs.
"""
import json
import uuid
from datetime import datetime, timezone

from config.connection import run_sql_statement, QueryFailedError

HISTORY_TABLE = "workspace.default.pipeline_remediation_rca_history"


def ensure_history_table_exists() -> None:
    """Creates the table if it doesn't exist. Safe to call every run."""
    run_sql_statement(
        f"""
        CREATE TABLE IF NOT EXISTS {HISTORY_TABLE} (
            investigation_id STRING,
            run_id STRING,
            job_id STRING,
            job_name STRING,
            started_at TIMESTAMP,
            root_cause_status STRING,
            root_cause_text STRING,
            confidence STRING,
            tool_calls_made ARRAY<STRING>,
            hypotheses_json STRING,
            conversation_log_json STRING,
            evidence_unavailable ARRAY<STRING>
        )
        """
    )


def save_rca_history(signal, rca_result) -> str:
    """
    Writes one row per investigation. Returns the investigation_id so
    it can be referenced later (e.g. from the HITL approval record, or
    a follow-up query "show me every investigation where a hypothesis
    was ruled out incorrectly").
    """
    ensure_history_table_exists()

    investigation_id = str(uuid.uuid4())
    started_at = datetime.now(timezone.utc).isoformat()

    hypotheses_json = json.dumps(
        [
            {
                "id": h.id,
                "statement": h.statement,
                "status": h.status.value,
                "supporting_evidence": h.supporting_evidence,
                "contradicting_evidence": h.contradicting_evidence,
            }
            for h in rca_result.hypotheses
        ]
    )

    conversation_log_json = json.dumps(rca_result.conversation_log)

    tool_calls_sql = "ARRAY(" + ", ".join(
        f"'{tc}'" for tc in rca_result.tool_calls_made
    ) + ")" if rca_result.tool_calls_made else "ARRAY()"

    evidence_gap_sql = "ARRAY(" + ", ".join(
        f"'{e[:200].replace(chr(39), chr(39)+chr(39))}'"  # escape single quotes
        for e in rca_result.evidence_unavailable
    ) + ")" if rca_result.evidence_unavailable else "ARRAY()"

    # Escape single quotes in free-text fields before inline SQL insert.
    def esc(s: str) -> str:
        return (s or "").replace("'", "''")

    run_sql_statement(
        f"""
        INSERT INTO {HISTORY_TABLE} VALUES (
            '{investigation_id}',
            '{esc(signal.run_id)}',
            '{esc(signal.job_id or "")}',
            '{esc(signal.job_name or "")}',
            TIMESTAMP '{started_at}',
            '{esc(rca_result.root_cause_status)}',
            '{esc(rca_result.root_cause_text)}',
            '{esc(rca_result.confidence.value)}',
            {tool_calls_sql},
            '{esc(hypotheses_json)}',
            '{esc(conversation_log_json)}',
            {evidence_gap_sql}
        )
        """
    )

    return investigation_id


def load_rca_history(run_id: str) -> list[dict]:
    """Look up every past investigation for a given run_id."""
    rows = run_sql_statement(
        f"SELECT * FROM {HISTORY_TABLE} WHERE run_id = '{run_id}' ORDER BY started_at DESC"
    )
    return rows


def load_investigation(investigation_id: str) -> dict | None:
    """
    Full replay of one investigation — including the complete
    conversation_log, so you can see exactly what Claude reasoned and
    which tools it called, in order, for any past run.
    """
    rows = run_sql_statement(
        f"SELECT * FROM {HISTORY_TABLE} WHERE investigation_id = '{investigation_id}'"
    )
    return rows[0] if rows else None
