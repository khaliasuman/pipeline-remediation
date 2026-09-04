"""
Step 4 of pipeline-incident-remediation: genuine multi-turn, agentic
tool-calling RCA, using Claude directly via the external Anthropic API
(not Databricks Model Serving — Databricks' own docs confirm reliable
multi-turn function calling is a Claude-specific capability; the Llama
endpoints available in this workspace are documented as single-turn
only, still under development for multi-turn).

This is the real, Omnigent-style pattern: Claude decides which tool to
call, your code executes it, the result feeds back, and Claude decides
its next move — genuinely agentic, not a Python-scripted decision tree
asking the model to pick from a fixed menu each turn.

The hard rule this file does NOT change: hitl_gate.py still has no
auto-approve path. Claude can investigate freely; it can never apply a
fix. That boundary is enforced by which tools exist below — there is no
"apply_fix" or "write_file" tool defined here at all.
"""
import json
import os
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

import anthropic

from config.connection import databricks_get, run_sql_statement, QueryFailedError
from src.detect import FailureSignal

SKILL_PATH = Path(__file__).parent.parent / "SKILL.md"
MAX_TOOL_TURNS = 8


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
    root_cause_status: str
    root_cause_text: str
    confidence: Confidence
    evidence_unavailable: list[str] = field(default_factory=list)
    jobs_affected: list[str] = field(default_factory=list)
    contained_to_single_job: bool = True
    lineage_hops_traversed: int = 0
    hop_extension_flagged: bool = False
    requires_human_judgment: list[str] = field(default_factory=list)
    conversation_log: list[dict] = field(default_factory=list)
    tool_calls_made: list[str] = field(default_factory=list)  # audit trail


# ── Real tools Claude can call — READ-ONLY, no exceptions ────────────
# There is deliberately no tool here that writes, retriggers, backfills,
# or applies anything. If you're tempted to add one, don't — that
# belongs behind hitl_gate.py's explicit human approval, not inside a
# tool Claude can reach mid-investigation.

TOOLS = [
    {
        "name": "get_task_output",
        "description": (
            "Get the real, detailed error output for a specific failed "
            "task run — not just the run-level summary message. Use "
            "this first, always, before generating hypotheses from a "
            "generic 'Workload failed' message alone."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"task_run_id": {"type": "string"}},
            "required": ["task_run_id"],
        },
    },
    {
        "name": "get_run_history",
        "description": "Get the last 10 runs for a job, to check if a failure is recurring vs. a one-off.",
        "input_schema": {
            "type": "object",
            "properties": {"job_id": {"type": "string"}},
            "required": ["job_id"],
        },
    },
    {
        "name": "get_table_lineage",
        "description": "Get downstream tables that read from a source table, to test whether a failure's blast radius crosses job boundaries.",
        "input_schema": {
            "type": "object",
            "properties": {"table_name": {"type": "string"}},
            "required": ["table_name"],
        },
    },
    {
        "name": "get_table_schema_history",
        "description": "Get DESCRIBE HISTORY for a Delta table, to check for a recent schema-changing commit.",
        "input_schema": {
            "type": "object",
            "properties": {"table_name": {"type": "string"}},
            "required": ["table_name"],
        },
    },
    {
        "name": "report_hypothesis_status",
        "description": (
            "Report the current status of every hypothesis after "
            "reviewing new evidence. You must call this after every "
            "tool result, before deciding your next action. Do not "
            "skip this — it is how your reasoning gets recorded, not "
            "just your final answer."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "hypotheses": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string"},
                            "statement": {"type": "string"},
                            "status": {
                                "type": "string",
                                "enum": ["leading", "contested", "ruled_out"],
                            },
                            "reason": {"type": "string"},
                        },
                        "required": ["id", "statement", "status", "reason"],
                    },
                }
            },
            "required": ["hypotheses"],
        },
    },
    {
        "name": "finalize_rca",
        "description": (
            "Call this ONLY when the stopping criteria from SKILL.md "
            "are genuinely met: exactly one hypothesis leading, all "
            "others explicitly ruled_out, no evidence gap the leading "
            "hypothesis depends on. If you're not there yet, keep "
            "investigating instead of calling this early."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "status": {"type": "string", "enum": ["established", "working"]},
                "root_cause": {"type": "string"},
                "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
            },
            "required": ["status", "root_cause", "confidence"],
        },
    },
]


def _load_skill() -> str:
    if not SKILL_PATH.exists():
        raise FileNotFoundError(f"SKILL.md not found at {SKILL_PATH}")
    return SKILL_PATH.read_text()


def _execute_tool(name: str, tool_input: dict, signal: FailureSignal) -> str:
    """
    The only place real API calls actually happen. Claude decides WHAT
    to call; this function is the ONLY thing that actually calls it —
    Claude never gets raw credentials or a direct execution path.
    """
    try:
        if name == "get_task_output":
            output = databricks_get(
                "/api/2.1/jobs/runs/get-output",
                params={"run_id": tool_input["task_run_id"]},
            )
            error = output.get("error") or output.get("notebook_output", {}).get("result", "")
            return json.dumps({"error_output": str(error)[:2000]})

        if name == "get_run_history":
            history = databricks_get(
                "/api/2.1/jobs/runs/list",
                params={"job_id": tool_input["job_id"], "limit": 10},
            )
            runs = history.get("runs", [])
            failed = sum(1 for r in runs if r.get("state", {}).get("result_state") == "FAILED")
            return json.dumps({"total_checked": len(runs), "failed_count": failed})

        if name == "get_table_lineage":
            rows = run_sql_statement(
                f"""SELECT downstream_table_name FROM system.access.table_lineage
                    WHERE source_table_name LIKE '%{tool_input["table_name"]}%' LIMIT 10"""
            )
            return json.dumps({"downstream_tables": [r[0] for r in rows if r]})

        if name == "get_table_schema_history":
            rows = run_sql_statement(f"DESCRIBE HISTORY {tool_input['table_name']} LIMIT 10")
            return json.dumps({"history": [str(r) for r in rows]})

        return json.dumps({"error": f"Unknown tool: {name}"})

    except QueryFailedError as e:
        # Fail loudly, but as a tool RESULT Claude can see and reason
        # about — not a crash. Claude must record this as
        # evidence_unavailable, not silently ignore it.
        return json.dumps({"error": f"Tool call failed: {e}"})


def run_rca(signal: FailureSignal, error_text: str = "") -> RCAResult:
    """
    Genuine multi-turn agentic tool-calling, via the external Anthropic
    API directly. Claude decides each tool call; this function executes
    it and feeds the real result back, repeating until Claude calls
    finalize_rca or MAX_TOOL_TURNS is reached.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError(
            "ANTHROPIC_API_KEY not set. This RCA path uses the external "
            "Anthropic API directly, not Databricks Model Serving — set "
            "this env var before calling run_rca()."
        )
    client = anthropic.Anthropic(api_key=api_key)

    system_prompt = (
        _load_skill()
        + "\n\n---\n\nYou are running Step 4 (RCA) now, for a real "
        "Databricks failure. Use the tools available to you to "
        "investigate — always start with get_task_output to see the "
        "REAL error, not just the generic summary. Call "
        "report_hypothesis_status after every piece of evidence. Only "
        "call finalize_rca once the stopping criteria from SKILL.md are "
        "genuinely met — you may NOT report status: established while "
        "any hypothesis remains contested."
    )

    messages = [
        {
            "role": "user",
            "content": (
                f"run_id={signal.run_id}, job_id={signal.job_id}, "
                f"job_name={signal.job_name}\n\n"
                f"Initial (possibly generic) error: {error_text or signal.state_message}\n\n"
                "Investigate and diagnose this."
            ),
        }
    ]

    hypotheses: list[Hypothesis] = []
    evidence_unavailable: list[str] = []
    jobs_affected = [signal.job_id] if signal.job_id else []
    contained_to_single_job = True
    tool_calls_made: list[str] = []
    final_result: dict | None = None

    for turn in range(MAX_TOOL_TURNS):
        response = client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=1024,
            system=system_prompt,
            tools=TOOLS,
            messages=messages,
        )

        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason != "tool_use":
            break  # Claude responded with plain text, no more tool calls

        tool_results = []
        for block in response.content:
            if block.type != "tool_use":
                continue

            tool_calls_made.append(block.name)

            if block.name == "report_hypothesis_status":
                hypotheses = [
                    Hypothesis(
                        id=h["id"],
                        statement=h["statement"],
                        status=HypothesisStatus(h["status"]),
                        supporting_evidence=[h["reason"]] if h["status"] == "leading" else [],
                        contradicting_evidence=[h["reason"]] if h["status"] == "ruled_out" else [],
                    )
                    for h in block.input["hypotheses"]
                ]
                result_text = "Recorded."

            elif block.name == "finalize_rca":
                final_result = block.input
                result_text = "Finalized."

            else:
                result_text = _execute_tool(block.name, block.input, signal)
                if '"error"' in result_text:
                    evidence_unavailable.append(f"{block.name}: {result_text}")
                if block.name == "get_table_lineage":
                    parsed = json.loads(result_text)
                    if parsed.get("downstream_tables"):
                        jobs_affected.extend(parsed["downstream_tables"])
                        contained_to_single_job = False

            tool_results.append(
                {"type": "tool_result", "tool_use_id": block.id, "content": result_text}
            )

        messages.append({"role": "user", "content": tool_results})

        if final_result:
            break

    root_cause_status, root_cause_text, confidence = _resolve(
        hypotheses, evidence_unavailable, final_result
    )

    return RCAResult(
        error_type=_infer_error_type(error_text or signal.state_message),
        failure_mechanism=root_cause_text,
        hypotheses=hypotheses,
        root_cause_status=root_cause_status,
        root_cause_text=root_cause_text,
        confidence=confidence,
        evidence_unavailable=evidence_unavailable,
        jobs_affected=jobs_affected,
        contained_to_single_job=contained_to_single_job,
        conversation_log=[
            {"role": m["role"], "content": str(m["content"])[:500]} for m in messages
        ],
        tool_calls_made=tool_calls_made,
    )


def _resolve(
    hypotheses: list[Hypothesis],
    evidence_unavailable: list[str],
    final_result: dict | None,
) -> tuple[str, str, Confidence]:
    """
    Enforced in code — Claude's own finalize_rca call is a CLAIM, not
    the final word. This function independently checks whether the
    stopping criteria actually hold before trusting 'established'.
    """
    leading = [h for h in hypotheses if h.status == HypothesisStatus.LEADING]
    contested = [h for h in hypotheses if h.status == HypothesisStatus.CONTESTED]

    genuinely_established = (
        len(leading) == 1 and not contested and not evidence_unavailable
    )

    if final_result and final_result.get("status") == "established":
        if not genuinely_established:
            # Claude claimed established, but the recorded hypothesis
            # statuses don't actually support it — downgrade rather
            # than trust the claim at face value.
            return (
                "working",
                f"[DOWNGRADED — claimed established but evidence doesn't "
                f"support it] {final_result.get('root_cause', '')}",
                Confidence.LOW,
            )
        confidence = Confidence.HIGH if len(leading[0].supporting_evidence) >= 1 else Confidence.MEDIUM
        return "established", final_result["root_cause"], confidence

    if final_result:
        return "working", final_result.get("root_cause", ""), Confidence.MEDIUM

    if genuinely_established:
        return "established", leading[0].statement, Confidence.HIGH

    return (
        "working",
        "RCA did not reach a finalized conclusion within the turn limit.",
        Confidence.LOW,
    )


def _infer_error_type(error_text: str) -> str:
    mapping = {
        "PATH_NOT_FOUND": "file_path", "SCHEMA": "schema", "PARSE": "sql",
        "TIMEOUT": "timeout", "TIMED OUT": "timeout",
        "SparkInternalError": "spark_compute", "KeyError": "python_application",
        "AttributeError": "python_application", "INSUFFICIENT_PERMISSIONS": "permission",
        "ACCESS_TO_TABLE_BLOCKED": "table_access_mode",
        "DATA_QUALITY_VIOLATION": "data_quality", "SQLSTATE": "sql",
        "zero records": "empty_output",
    }
    for key, category in mapping.items():
        if key.lower() in error_text.lower():
            return category
    return "unknown"
