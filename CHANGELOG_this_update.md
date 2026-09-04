# Files new or updated in this round

## New files
- `src/rca_agentic.py` — replaces the earlier rca.py's manual-loop
  version. Real, multi-turn Claude tool-calling via the external
  Anthropic API directly (not Databricks Model Serving — Databricks'
  own docs confirm reliable multi-turn function calling is currently a
  Claude-specific capability, not yet reliable on the Llama endpoints
  available in this workspace). Uses claude-haiku-4-5 by default.
  Defines 5 real, READ-ONLY tools Claude can call (get_task_output,
  get_run_history, get_table_lineage, get_table_schema_history,
  report_hypothesis_status) plus finalize_rca. There is deliberately no
  write/apply/retrigger tool anywhere in this file — that boundary is
  structural, not just instructional.

- `src/history.py` — persists every investigation (hypotheses, tool
  calls made, full conversation log) to a real Delta table
  (workspace.default.pipeline_remediation_rca_history), so past
  investigations are queryable and auditable, not just visible in
  notebook output while they run.

- `notebooks/interactive_notebook.py` — notebook-native interactive
  flow. No separate Databricks App, no extra infrastructure or cost.
  Uses dbutils.widgets for both the run_id input and an explicit
  approve/reject decision widget. Live output prints as Claude
  investigates, cell by cell. Still never applies a fix automatically
  — approving via the widget prints instructions for the real PR
  process, not an auto-apply action.

## Updated files
- `requirements.txt` — added `anthropic>=0.40.0`

## Unchanged (still exactly as originally built)
config/connection.py, src/classify.py, src/recommend.py,
src/hitl_gate.py, src/validate.py, src/detect.py, SKILL.md, tests/,
docs/, README.md

## To install
1. Copy src/rca_agentic.py, src/history.py, notebooks/interactive_notebook.py
   into your existing repo at the same relative paths.
2. Replace requirements.txt with the updated version.
3. Set ANTHROPIC_API_KEY — ideally via dbutils.secrets, not pasted
   directly into a notebook. See notebooks/interactive_notebook.py's
   auth cell for the exact pattern.
4. git add . && git commit -m "Add agentic RCA via Claude, history
   persistence, notebook-native interactive flow" && git push
5. Pull into your Databricks Repo, open notebooks/interactive_notebook.py,
   enter a run_id in the widget, run cells top to bottom.
