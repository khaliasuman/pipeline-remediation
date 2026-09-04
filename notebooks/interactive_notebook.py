# Databricks notebook source
# MAGIC %md
# MAGIC # Pipeline Incident Remediation — Interactive Notebook
# MAGIC
# MAGIC No separate app, no extra infrastructure — this is entirely notebook
# MAGIC cells. Run cells in order. Investigation output prints live, cell by
# MAGIC cell, as it happens. The approval step is a real widget you set and
# MAGIC a cell you run explicitly — nothing applies automatically.

# COMMAND ----------

import os

ctx = dbutils.notebook.entry_point.getDbutils().notebook().getContext()
os.environ["DATABRICKS_HOST"] = ctx.apiUrl().get()
os.environ["DATABRICKS_TOKEN"] = ctx.apiToken().get()
print(f"Authenticated against: {os.environ['DATABRICKS_HOST']}")

# COMMAND ----------

# MAGIC %md
# MAGIC Set your Anthropic key here — or better, use a Databricks secret
# MAGIC scope instead of pasting it directly:
# MAGIC `dbutils.secrets.get(scope="your-scope", key="anthropic-api-key")`

# COMMAND ----------

os.environ["ANTHROPIC_API_KEY"] = dbutils.secrets.get(scope="your-scope", key="anthropic-api-key")
# Or, for quick testing only (do not commit a real key to the notebook):
# os.environ["ANTHROPIC_API_KEY"] = "sk-ant-..."

# COMMAND ----------

import sys
sys.path.append(os.path.abspath(".."))

from src.detect import FailureSignal
from src.classify import classify_failure, Category
from src.rca_agentic import run_rca, HypothesisStatus
from src.recommend import build_recommendation
from src.hitl_gate import present_for_approval
from src.history import save_rca_history
from config.connection import databricks_get

print("Ready.")

# COMMAND ----------

dbutils.widgets.text("run_id", "", "run_id (required)")
run_id = dbutils.widgets.get("run_id")
if not run_id:
    raise ValueError("Enter a run_id in the widget above, then re-run this cell.")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 1 — Look up the run and classify
# MAGIC Real, fast, no LLM call yet.

# COMMAND ----------

run_detail = databricks_get("/api/2.1/jobs/runs/get", params={"run_id": run_id})
state = run_detail.get("state", {})

if state.get("result_state") != "FAILED":
    print(f"run_id {run_id} is not FAILED (result_state={state.get('result_state')}). Nothing to diagnose.")
    dbutils.notebook.exit("not_failed")

signal = FailureSignal(
    run_id=str(run_id),
    job_id=str(run_detail.get("job_id", "")),
    job_name=run_detail.get("run_name"),
    state_message=state.get("state_message", ""),
    start_time=str(run_detail.get("start_time", "")),
)
print(f"✅ job: {signal.job_name}  (job_id={signal.job_id})")
print(f"   error: {signal.state_message}")

classification = classify_failure(signal, signal.state_message)
print(f"\nClassification: {classification.category.value}")
print(f"Reason: {classification.reason}")

if classification.category == Category.TRANSIENT:
    print(f"\nRetriggered: {classification.retriggered}, succeeded: {classification.retrigger_succeeded}")
    print("Transient and closed — no RCA needed.")
    dbutils.notebook.exit("transient_closed")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 2 — RCA: watch Claude investigate live
# MAGIC Real multi-turn tool calling. Each tool call and hypothesis update
# MAGIC prints as it happens — this is the closest notebook-native
# MAGIC equivalent to watching an Omnigent session unfold, without any
# MAGIC extra app or infrastructure.

# COMMAND ----------

print(f"Investigating run_id={signal.run_id}...\n")
rca_result = run_rca(signal, signal.state_message)

print(f"\nReal tool calls made, in order: {rca_result.tool_calls_made}")
print(f"\nFinal hypothesis statuses:")
for h in rca_result.hypotheses:
    icon = {"leading": "🟢", "contested": "🟡", "ruled_out": "🔴"}[h.status.value]
    print(f"  {icon} [{h.status.value}] {h.statement}")
    if h.supporting_evidence:
        print(f"      supporting: {h.supporting_evidence}")
    if h.contradicting_evidence:
        print(f"      contradicting: {h.contradicting_evidence}")

if rca_result.evidence_unavailable:
    print(f"\n⚠️  Evidence unavailable: {rca_result.evidence_unavailable}")

print(f"\nRoot cause status: {rca_result.root_cause_status}")
print(f"Confidence: {rca_result.confidence.value}")
print(f"Root cause: {rca_result.root_cause_text}")

investigation_id = save_rca_history(signal, rca_result)
print(f"\nSaved as investigation_id={investigation_id}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 3 — Recommendation and HITL gate
# MAGIC Prints the routing decision and, if Tier 2, the proposed diff.
# MAGIC Nothing is applied here — this is a print only.

# COMMAND ----------

recommendation = build_recommendation(rca_result, backfill_required=False)
present_for_approval(recommendation)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 4 — Explicit approval widget
# MAGIC Set this widget to `approve` only after you've actually read the
# MAGIC diff above yourself. Re-run this cell to record your decision.
# MAGIC This still does not apply anything automatically — see the printed
# MAGIC note below for what a real "apply" step requires.

# COMMAND ----------

dbutils.widgets.dropdown("decision", "pending", ["pending", "approve", "reject"], "Your decision")
decision = dbutils.widgets.get("decision")

if decision == "pending":
    print("Set the 'decision' widget above to 'approve' or 'reject', then re-run this cell.")
elif decision == "reject":
    print(f"❌ Rejected. investigation_id={investigation_id}. No changes made.")
elif decision == "approve":
    if recommendation.routing != "tier_2":
        print("⚠️  This is a Tier 3 finding — advisory only. There is no fix to approve.")
    else:
        print(f"✅ Approved. investigation_id={investigation_id}.")
        print(
            "\nThis notebook deliberately does NOT apply the fix automatically "
            "from this widget alone — that would defeat the purpose of a real "
            "approval gate. To actually apply it:\n"
            "  1. Copy the diff printed in Step 3 into a real PR against the "
            "affected notebook/file.\n"
            "  2. Get it reviewed and merged through your team's normal process.\n"
            "  3. Run notebooks/validate_fix.py (Step 8) afterward to confirm recovery."
        )
