# Databricks notebook source
# MAGIC %md
# MAGIC # Pipeline Incident Remediation — Entry Point
# MAGIC
# MAGIC Runs the full Step 1-6 workflow against real Databricks job failures.
# MAGIC See `../SKILL.md` for the full behavior contract this notebook implements.
# MAGIC
# MAGIC **This notebook never applies a fix automatically.** Tier 2 fixes stop
# MAGIC at the HITL gate and require a separate, explicit approval action.
# MAGIC
# MAGIC Run cells top to bottom in one pass (**Run All**). Re-running individual
# MAGIC cells out of order can lose the auth env vars set in the first cell.

# COMMAND ----------

# MAGIC %md
# MAGIC ### Auth setup — must run before anything else
# MAGIC Inside a Databricks notebook there is no `~/.databrickscfg` file on
# MAGIC the compute. Use the notebook's own native auth context instead —
# MAGIC `config/connection.py` checks these env vars first, before falling
# MAGIC back to `~/.databrickscfg` for local/non-notebook execution.

# COMMAND ----------

import os

ctx = dbutils.notebook.entry_point.getDbutils().notebook().getContext()
os.environ["DATABRICKS_HOST"] = ctx.apiUrl().get()
os.environ["DATABRICKS_TOKEN"] = ctx.apiToken().get()

print(f"Authenticated against: {os.environ['DATABRICKS_HOST']}")

# COMMAND ----------

# MAGIC %md
# MAGIC ### LLM endpoint configuration
# MAGIC This workspace does not have a Claude/GPT serving endpoint — set to
# MAGIC the real, confirmed-available endpoints instead. Cheap model for
# MAGIC classification/first-pass hypothesis generation, larger model for
# MAGIC escalation, matching the cost-tiering design in SKILL.md.
# MAGIC
# MAGIC Confirmed available in this workspace: `databricks-gpt-oss-120b`,
# MAGIC `databricks-gpt-oss-20b`, `databricks-qwen3-next-80b-a3b-instruct`,
# MAGIC `databricks-qwen35-122b-a10b`, `databricks-llama-4-maverick`,
# MAGIC `databricks-gemma-3-12b`, `databricks-meta-llama-3-1-8b-instruct`,
# MAGIC `databricks-meta-llama-3-3-70b-instruct`. Re-run the check cell below
# MAGIC if your workspace's available endpoints differ.

# COMMAND ----------

os.environ["PIPELINE_REMEDIATION_LLM_ENDPOINT"] = "databricks-meta-llama-3-1-8b-instruct"
os.environ["PIPELINE_REMEDIATION_ESCALATION_ENDPOINT"] = "databricks-meta-llama-3-3-70b-instruct"

print(f"First-pass model: {os.environ['PIPELINE_REMEDIATION_LLM_ENDPOINT']}")
print(f"Escalation model: {os.environ['PIPELINE_REMEDIATION_ESCALATION_ENDPOINT']}")

# COMMAND ----------

# MAGIC %md
# MAGIC ### Optional — re-verify available serving endpoints
# MAGIC Run this if you're unsure the two endpoint names above are still
# MAGIC valid for this workspace.

# COMMAND ----------

import requests

resp = requests.get(
    f"{os.environ['DATABRICKS_HOST']}/api/2.0/serving-endpoints",
    headers={"Authorization": f"Bearer {os.environ['DATABRICKS_TOKEN']}"},
    timeout=15,
)
if resp.status_code == 200:
    endpoints = [e["name"] for e in resp.json().get("endpoints", [])]
    print("Available serving endpoints:", endpoints)
    for required in (
        os.environ["PIPELINE_REMEDIATION_LLM_ENDPOINT"],
        os.environ["PIPELINE_REMEDIATION_ESCALATION_ENDPOINT"],
    ):
        status = "✓ found" if required in endpoints else "✗ MISSING — update the cell above"
        print(f"  {required}: {status}")
else:
    print(f"Could not list serving endpoints: {resp.status_code} {resp.text[:200]}")

# COMMAND ----------

# MAGIC %md
# MAGIC ### Import the workflow modules

# COMMAND ----------

import sys

sys.path.append(os.path.abspath(".."))

from src.detect import fetch_recent_failures, split_incident_text
from src.classify import classify_failure, Category
from src.rca import run_rca
from src.recommend import build_recommendation
from src.hitl_gate import present_for_approval

print("Modules imported successfully.")

# COMMAND ----------

dbutils.widgets.text("run_id", "", "Specific run_id (optional — leave blank to scan for all recent failures)")
target_run_id = dbutils.widgets.get("run_id")

# COMMAND ----------

# MAGIC %md
# MAGIC ### Step 1-2: Detect recent failures
# MAGIC Cheap, deterministic — no LLM call yet.

# COMMAND ----------

failures = fetch_recent_failures()

if target_run_id:
    failures = [f for f in failures if f.run_id == target_run_id]

if not failures:
    print("No new failures found.")
    dbutils.notebook.exit("no_failures")

print(f"Found {len(failures)} failure(s) to process.")
for f in failures:
    print(f"  run_id={f.run_id} job_id={f.job_id} job_name={f.job_name}")

# COMMAND ----------

# MAGIC %md
# MAGIC ### Step 2-6: Classify, RCA, recommend, and stop at the HITL gate
# MAGIC Nothing in this loop applies a fix. Tier 2 findings print a
# MAGIC proposed diff for review; Tier 3 findings print advisory-only
# MAGIC findings. Both require a separate, explicit human approval step
# MAGIC outside this notebook before anything is applied.

# COMMAND ----------

for signal in failures:
    print(f"\n{'='*70}")
    print(f"Processing run_id={signal.run_id} job_id={signal.job_id}")
    print(f"{'='*70}")

    error_blocks = split_incident_text(signal.state_message)

    for error_text in error_blocks:
        classification = classify_failure(signal, error_text)
        print(f"\nClassification: {classification.category.value}")
        print(f"Reason: {classification.reason}")

        if classification.category == Category.TRANSIENT:
            print(f"Retriggered: {classification.retriggered}, "
                  f"succeeded: {classification.retrigger_succeeded}")
            continue  # Tier 1, closed — no RCA needed for a genuine transient

        try:
            rca_result = run_rca(signal, error_text)
        except Exception as e:
            print(f"RCA failed: {e}")
            continue

        print(f"\nRCA status: {rca_result.root_cause_status}")
        print(f"RCA confidence: {rca_result.confidence.value}")
        for h in rca_result.hypotheses:
            print(f"  [{h.status.value}] {h.statement}")
        if rca_result.evidence_unavailable:
            print(f"  Evidence unavailable: {rca_result.evidence_unavailable}")

        recommendation = build_recommendation(
            rca_result, backfill_required=False  # detect this from RCA evidence in production use
        )

        print("\n--- HITL GATE ---")
        present_for_approval(recommendation)
        print("--- Nothing applied. Awaiting explicit human approval. ---\n")