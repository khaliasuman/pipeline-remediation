"""
Step 5 of pipeline-incident-remediation: build a structured
recommendation from an RCA result. Separates mechanical fixes (safe to
apply once approved) from judgment calls (the agent cannot decide).
Applies the mechanical routing rule — the agent never overrides this
with its own sense of whether a case "feels" simple.
"""
from dataclasses import dataclass, field

from config.connection import call_llm
from src.rca import RCAResult, Confidence


@dataclass
class BackfillPlan:
    time_window_start: str
    time_window_end: str
    downstream_replay_needed: list[str]
    precondition: str = (
        "Checkpoint/offset must be validated before any backfill runs, "
        "or the missing window is silently skipped rather than reprocessed."
    )


@dataclass
class Recommendation:
    root_cause_status: str
    confidence: Confidence
    mechanical_fixes: list[dict] = field(default_factory=list)
    diagnostic_action_required: list[str] = field(default_factory=list)
    judgment_calls: list[dict] = field(default_factory=list)
    backfill_required: bool = False
    backfill_plan: BackfillPlan | None = None
    routing: str = "tier_3"  # "tier_2" | "tier_3"


def build_recommendation(rca: RCAResult, backfill_required: bool = False) -> Recommendation:
    if rca.root_cause_status != "established":
        # A fix proposed against an unestablished cause is worse than
        # no fix — output diagnostic actions instead, route to Tier 3.
        return Recommendation(
            root_cause_status=rca.root_cause_status,
            confidence=rca.confidence,
            diagnostic_action_required=_diagnostic_actions(rca),
            judgment_calls=[
                {
                    "question": "What further evidence would establish root cause?",
                    "why_it_matters": (
                        "No fix can be proposed until root cause status "
                        "reaches 'established' — see rca.py's stopping criteria."
                    ),
                }
            ],
            routing="tier_3",
        )

    mechanical_fixes = _propose_mechanical_fix(rca)
    judgment_calls = _identify_judgment_calls(rca)

    routing = _apply_routing_rule(
        root_cause_established=True,
        judgment_calls_empty=not judgment_calls,
        confidence_high=rca.confidence == Confidence.HIGH,
        contained_to_single_job=rca.contained_to_single_job,
        backfill_required=backfill_required,
    )

    backfill_plan = None
    if backfill_required:
        backfill_plan = BackfillPlan(
            time_window_start="<derive from RCA evidence>",
            time_window_end="<derive from RCA evidence>",
            downstream_replay_needed=rca.jobs_affected,
        )

    return Recommendation(
        root_cause_status=rca.root_cause_status,
        confidence=rca.confidence,
        mechanical_fixes=mechanical_fixes,
        judgment_calls=judgment_calls,
        backfill_required=backfill_required,
        backfill_plan=backfill_plan,
        routing=routing,
    )


def _apply_routing_rule(
    root_cause_established: bool,
    judgment_calls_empty: bool,
    confidence_high: bool,
    contained_to_single_job: bool,
    backfill_required: bool,
) -> str:
    """
    Mechanical check — never overridden by the agent's own sense of
    whether a case "feels" simple. backfill_required alone forces
    tier_3 even if everything else is clean.
    """
    if (
        root_cause_established
        and judgment_calls_empty
        and confidence_high
        and contained_to_single_job
        and not backfill_required
    ):
        return "tier_2"
    return "tier_3"


def _propose_mechanical_fix(rca: RCAResult) -> list[dict]:
    prompt = f"""Root cause established: {rca.root_cause_text}

Propose a minimal, scoped code fix (a diff) that directly addresses this
root cause. Do not propose anything beyond what the root cause requires.
Respond with a short description and the diff only."""
    response = call_llm(prompt, use_escalation_model=False)
    return [{"description": rca.root_cause_text, "diff": response}]


def _identify_judgment_calls(rca: RCAResult) -> list[dict]:
    calls = []
    for item in rca.requires_human_judgment:
        calls.append({"question": item, "why_it_matters": "Flagged in RCA."})
    return calls


def _diagnostic_actions(rca: RCAResult) -> list[str]:
    actions = []
    for gap in rca.evidence_unavailable:
        actions.append(f"Resolve evidence gap: {gap}")
    contested_count = sum(1 for h in rca.hypotheses if h.status.value == "contested")
    if contested_count:
        actions.append(
            f"{contested_count} hypothesis/es still contested — add structured "
            "logging or capture a full stack trace to discriminate further."
        )
    if not actions:
        actions.append("No hypothesis reached leading status — re-run RCA with a wider evidence net.")
    return actions
