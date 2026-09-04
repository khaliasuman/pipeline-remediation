"""
Step 6 of pipeline-incident-remediation: the mandatory human-in-the-
loop gate. No exceptions. This is the actual product, not a formality —
most real-world "fix wasn't fine" incidents in this domain come from
skipping or rubber-stamping this step.

This module deliberately contains NO code path that applies a fix
without an explicit, externally-supplied approval token. There is no
"auto-approve" flag anywhere in this file, on purpose.
"""
from dataclasses import dataclass

from src.recommend import Recommendation


@dataclass
class GateResult:
    approved: bool
    approver: str
    notes: str = ""


def present_for_approval(recommendation: Recommendation) -> str:
    """
    Formats the recommendation for human review. Returns the text to
    show — this function never itself decides approval. Call
    request_approval() separately once a human has actually reviewed this.
    """
    lines = [
        f"ROUTING: {recommendation.routing.upper()}",
        f"Root cause status: {recommendation.root_cause_status}",
        f"Confidence: {recommendation.confidence.value}",
        "",
    ]

    if recommendation.backfill_required:
        lines.append("⚠️  BACKFILL REQUIRED — this incident is not closed by a code fix alone.")
        if recommendation.backfill_plan:
            lines.append(f"  Time window: {recommendation.backfill_plan.time_window_start} "
                          f"to {recommendation.backfill_plan.time_window_end}")
            lines.append(f"  Downstream replay needed: {recommendation.backfill_plan.downstream_replay_needed}")
            lines.append(f"  Precondition: {recommendation.backfill_plan.precondition}")
        lines.append("")

    if recommendation.routing == "tier_3":
        lines.append("This is a TIER 3 finding — advisory only. No code will be")
        lines.append("applied automatically under any circumstance.")
        if recommendation.diagnostic_action_required:
            lines.append("\nDiagnostic actions needed before a fix can be proposed:")
            for action in recommendation.diagnostic_action_required:
                lines.append(f"  - {action}")
        if recommendation.judgment_calls:
            lines.append("\nJudgment calls requiring a human decision:")
            for jc in recommendation.judgment_calls:
                lines.append(f"  - {jc['question']} ({jc['why_it_matters']})")
    else:
        lines.append("This is a TIER 2 finding — a code fix is proposed below.")
        lines.append("It will NOT be applied until explicitly approved.")
        for fix in recommendation.mechanical_fixes:
            lines.append(f"\nProposed fix: {fix['description']}")
            lines.append(f"Diff:\n{fix['diff']}")

    text = "\n".join(lines)
    print(text)
    return text


def request_approval(recommendation: Recommendation, approver: str) -> GateResult:
    """
    Real approval must come from an actual human action outside this
    function — e.g. a PR review, a Slack approval workflow, or a
    Databricks Job task with a manual-approval step. This function is
    the single point every apply-side caller must go through; it is
    intentionally NOT a place where "yes" can be assumed by default.
    """
    if recommendation.routing != "tier_2":
        raise PermissionError(
            "Tier 3 findings are advisory only — this codebase provides "
            "no path to apply a Tier 3 recommendation automatically."
        )
    raise NotImplementedError(
        "Wire this to your team's real approval mechanism (PR review, "
        "Slack workflow, Databricks Job manual-approval task, etc.). "
        "This function must not be implemented as an automatic 'return "
        "GateResult(approved=True, ...)' — that would defeat the entire "
        "purpose of this gate."
    )
