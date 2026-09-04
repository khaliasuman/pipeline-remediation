# Databricks notebook source
# MAGIC %md
# MAGIC # Pipeline Incident Remediation — Entry Point
# MAGIC
# MAGIC Runs the full Step 1-6 workflow against real Databricks job failures.
# MAGIC See `../SKILL.md` for the full behavior contract this notebook implements.
# MAGIC
# MAGIC **This notebook never applies a fix automatically.** Tier 2 fixes stop
# MAGIC at the HITL gate and require a separate, explicit approval action.

# COMMAND ----------

import sys
import os

sys.path.append(os.path.abspath(".."))

from src.detect import fetch_recent_failures, split_incident_text
from src.classify import classify_failure, Category
from src.rca import run_rca
from src.recommend import build_recommendation
from src.hitl_gate import present_for_approval

# COMMAND ----------

dbutils.widgets.text("run_id", "", "Specific run_id (optional — leave blank to scan for all recent failures)")
target_run_id = dbutils.widgets.get("run_id")

# COMMAND ----------

failures = fetch_recent_failures()

if target_run_id:
    failures = [f for f in failures if f.run_id == target_run_id]

if not failures:
    print("No new failures found.")
    dbutils.notebook.exit("no_failures")

print(f"Found {len(failures)} failure(s) to process.")

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

        rca_result = run_rca(signal, error_text)
        print(f"\nRCA status: {rca_result.root_cause_status}")
        print(f"RCA confidence: {rca_result.confidence.value}")
        for h in rca_result.hypotheses:
            print(f"  [{h.status.value}] {h.statement}")

        recommendation = build_recommendation(
            rca_result, backfill_required=False  # detect this from RCA evidence in production use
        )

        print("\n--- HITL GATE ---")
        present_for_approval(recommendation)
        print("--- Nothing applied. Awaiting explicit human approval. ---\n")
