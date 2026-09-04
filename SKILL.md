---
name: pipeline-incident-remediation
description: Diagnose and remediate Databricks pipeline job failures — classify the failure, retrigger transient failures, run root-cause analysis, produce a structured fix recommendation, route to the correct human-in-the-loop gate before any code fix ships, and validate the fix once merged. Backfill gets a dry-run plan but is never executed — it always routes to a human handoff. Use this whenever a Databricks job/pipeline failure, incident, or defect summary is pasted in (error stack, SQLSTATE, "job failed", "backfill", "retrigger") — even if the user only asks "what happened" or "why did this fail." Also use when asked to build, extend, or reason about an agentic incident-remediation workflow for data pipelines. Do not use for general Databricks how-to questions unrelated to an active or past failure.
---

# Pipeline incident remediation

Turns a raw job-failure signal into one of three outcomes: auto-resolved, a
human-reviewable fix recommendation, or a full handoff — never a silent or
unreviewed production change.

## Core principle

This is a state machine with tiered autonomy, not one end-to-end agent call.
Route by **risk and reversibility**, not by pipeline stage.

| Tier | What | Autonomy |
|---|---|---|
| 1 | Classify, retrigger (if eligible), RCA, draft recommendation | Fully autonomous — reversible or read-only |
| 2 | Code fix application only | Agent proposes diff → human approves → agent executes |
| 3 | Cross-job scope, ambiguous schema-contract intent, low-confidence RCA, anything requiring backfill | Full handoff — agent output is advisory only |

**Backfill is out of scope for execution in this version.** Where a fix
needs a backfill, the agent identifies and flags that need but takes no
backfill action. Any `backfill_required: true` finding is an automatic
Tier 3 router regardless of how confident the rest of the recommendation
is — a fix that "requires backfill" is not something this skill closes
end-to-end yet.

Never skip a tier. Never let a Tier 2/3 action execute without the gate in
Step 6 having actually fired. A Tier 2 fix is not closed until Step 8
(Validation) confirms it.

## Connection rules

Never use `databricks-sql-connector` or `WorkspaceClient` — both confirmed
to hang indefinitely in production use. Always use the REST API directly
via `requests`, with an explicit timeout on every call. Never silently
substitute synthetic data on a query failure — fail loudly instead.

## Workflow

### Step 1 — Parse and split the failure signal

Real incident text often bundles multiple exceptions from different job
runs into one paste — e.g. a schema error on one run, followed by "no new
data available" on the retry. Do not treat pasted failure text as a single
signal. Split it into distinct exception blocks by timestamp/run before
classifying.

**Correlation check:** temporal proximity alone is not causality. Two
signals landing close together may still be independent failures — only
merge them into one incident if a dependency relationship, shared upstream
table/job, or matching error signature supports it. If unsupported, keep
them as separate incidents rather than forcing a single narrative.

### Step 2 — Classify

Tag the failure as `transient`, `structural`, or `ambiguous`.

- **`transient`** (safe to auto-retrigger): resource contention, cluster
  start timeouts, network blips, flaky-but-unchanged upstream availability.
- **`structural`** (never auto-remediate — go straight to RCA):
  `FIELD_NOT_FOUND`, `SCHEMA_MISMATCH`, `CANNOT_RESOLVE_COLUMN`, any 42xxx
  SQLSTATE, Auto Loader schema-evolution errors, or any error implying the
  source's actual shape changed.
- **`ambiguous`**: unfamiliar signature or conflicting split signals.
  Default to `confidence: low` rather than forcing a transient/structural
  call. Ties go to `structural` or `ambiguous` — the cost of an
  unnecessary RCA cycle is far lower than the cost of silent data loss
  from a wrongly-auto-remediated structural failure.

### Step 3 — Tier 1 auto-remediation (transient only, retrigger only)

Retrigger via the Databricks Jobs API. Deterministic — no LLM judgment in
the call itself. Backfill is not attempted here or anywhere in this
version. If retrigger fails, or classification was `structural`/
`ambiguous`, go straight to Step 4.

### Step 4 — RCA (always runs, read-only)

RCA never modifies anything. Query sources directly — never diagnose from
log text alone.

**First principle: the observed error is a symptom, not necessarily the
root cause.** Do not conclude root cause = the job/task where the error
surfaced without checking whether it's itself downstream of an earlier
failure.

**Hypothesis-driven traversal — generate, test, narrow, not exhaustive
traversal to a fixed depth:**

1. Generate a bounded set of hypotheses (typically 3-5) from the error
   signature and immediate context.
2. At each step, pick the source that best discriminates between the
   current leading hypotheses.
3. After each piece of evidence, update every hypothesis explicitly:
   `leading` (corroborating evidence from an independent source),
   `contested` (still plausible, unconfirmed), or `ruled_out` (evidence
   directly contradicts it, with a stated reason).
4. Lineage traversal is a form of evidence-gathering, not a separate
   phase. Continue past one hop only if that hop itself shows a
   failed/skipped/anomalous status in the same window. Default soft cap
   at 2 hops; extending past it requires human sign-off.
5. If a source is unavailable, record `evidence_unavailable` — never
   treat a missing check as evidence of "nothing wrong."

**Stopping criteria (all of these, not a numeric score):** exactly one
hypothesis `leading` with corroborating evidence from two independent
sources; no remaining hypothesis `contested`; no `evidence_unavailable`
touching evidence the leading hypothesis depends on. Otherwise, report the
state honestly as unresolved.

**Confidence:** `high` — one hypothesis leading, all others ruled out, no
relevant evidence gap. `medium` — one leading but an alternative still
contested, or a non-critical gap. `low` — no hypothesis reaches leading,
sources disagree, or scope needed a human-approved hop extension.

### Step 5 — Recommendation

Separates **mechanical fixes** (safe once approved) from **judgment
calls** (the agent cannot decide). If the root cause is still `working`
(stopping criteria not met), output `diagnostic_action_required` instead
of a fix framed against an unestablished cause — and route to Tier 3.

**Routing rule:** `tier_2` only if root-cause status is `established`,
`judgment_calls` is empty, `confidence: high`, `contained_to_single_job`
is `true`, and `backfill_required` is `false`. Otherwise `tier_3`.

### Step 6 — HITL gate (mandatory, no exceptions)

Tier 2: present the diff for approval. Tier 3: present RCA + recommendation
as findings only, explicitly call out any backfill need, and stop. Never
let Step 5's output execute directly.

### Step 7 — Execute (Tier 2 only, post-approval)

Apply the approved diff, open a PR with RCA + recommendation as the
description. Never auto-merge.

### Step 8 — Validation (after PR merge, Tier 2 only)

A merged PR is not proof of resolution. Check the job now completes, the
specific error signature doesn't recur, schema/field access matches
intent, and freshness/row-count sanity on the affected table. A `partial`
or `recurred` result reopens the incident.

## What this skill does not do

- Does not decide fix intent on ambiguous schema/contract questions.
- Does not retrigger, backfill, or auto-remediate structural failures.
- Does not execute a backfill under any circumstances — dry-run plan only.
- Does not touch source code or open a PR without explicit prior approval.
- Does not consider a Tier 2 incident closed until Step 8 confirms recovery.
