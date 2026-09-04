"""
Centralized connection configuration for pipeline-incident-remediation.

Both the Databricks connection and the LLM endpoint live here so
engineers have exactly one place to check or update credentials.

HARD RULES (confirmed via real production incidents, not optional):
1. Never use databricks-sql-connector or WorkspaceClient — both have
   hung indefinitely in real use on this pattern of environment. Always
   use the REST API directly via `requests`, with an explicit timeout.
2. Never silently substitute synthetic data when a query fails. Raise —
   a silent fallback that looks like a real result is worse than a
   visible error.
"""
import configparser
import os
import time
from dataclasses import dataclass
from typing import Any, Optional

import requests


class QueryFailedError(RuntimeError):
    """Raised when a Databricks call fails. Never caught to fabricate
    a fallback result — the caller must handle this explicitly."""


@dataclass
class DatabricksConfig:
    token: str
    host: str
    warehouse_id: str


def get_databricks_config() -> DatabricksConfig:
    """
    Reads Databricks credentials from ~/.databrickscfg. Raises if the
    file or required keys are missing — never returns a partially-valid
    config that would fail confusingly later.
    """
    config = configparser.ConfigParser()
    read_files = config.read(os.path.expanduser("~/.databrickscfg"))
    if not read_files:
        raise QueryFailedError(
            "~/.databrickscfg not found. Set it up before running this "
            "codebase — see README.md."
        )
    section = config["DEFAULT"]
    missing = [k for k in ("token", "host") if k not in section]
    if missing:
        raise QueryFailedError(f"~/.databrickscfg missing required keys: {missing}")

    return DatabricksConfig(
        token=section["token"],
        host=section["host"].rstrip("/"),
        warehouse_id=os.environ.get(
            "PIPELINE_REMEDIATION_WAREHOUSE_ID", "96b47259ff35cccf"
        ),
    )


def databricks_get(path: str, params: Optional[dict] = None, timeout: int = 15) -> dict:
    """
    GET against the Databricks REST API. Never use WorkspaceClient or
    databricks-sql-connector — this is the only sanctioned connection
    method for this codebase.
    """
    cfg = get_databricks_config()
    resp = requests.get(
        f"{cfg.host}{path}",
        headers={"Authorization": f"Bearer {cfg.token}"},
        params=params or {},
        timeout=timeout,
    )
    if resp.status_code != 200:
        raise QueryFailedError(
            f"GET {path} failed: {resp.status_code} {resp.text[:300]}"
        )
    return resp.json()


def databricks_post(path: str, json_body: dict, timeout: int = 30) -> dict:
    """POST against the Databricks REST API."""
    cfg = get_databricks_config()
    resp = requests.post(
        f"{cfg.host}{path}",
        headers={"Authorization": f"Bearer {cfg.token}"},
        json=json_body,
        timeout=timeout,
    )
    if resp.status_code != 200:
        raise QueryFailedError(
            f"POST {path} failed: {resp.status_code} {resp.text[:300]}"
        )
    return resp.json()


def run_sql_statement(statement: str, wait_seconds: int = 30) -> list:
    """
    Executes a SQL statement via the SQL Statement REST API and polls
    until it completes. Raises QueryFailedError on failure or timeout —
    never returns a fabricated empty/placeholder result.
    """
    cfg = get_databricks_config()
    result = databricks_post(
        "/api/2.0/sql/statements",
        {
            "statement": statement,
            "warehouse_id": cfg.warehouse_id,
            "wait_timeout": f"{wait_seconds}s",
        },
        timeout=wait_seconds + 10,
    )
    statement_id = result["statement_id"]

    for _ in range(60):
        state = result.get("status", {}).get("state")
        if state == "SUCCEEDED":
            return result.get("result", {}).get("data_array", [])
        if state in ("FAILED", "CANCELED"):
            raise QueryFailedError(
                f"SQL statement {state}: {result.get('status', {}).get('error', {})}"
            )
        time.sleep(1)
        result = databricks_get(f"/api/2.0/sql/statements/{statement_id}")

    raise QueryFailedError(
        "SQL statement did not complete in time — failing loudly, "
        "not fabricating a result."
    )


# ── LLM endpoint configuration ───────────────────────────────────────
@dataclass
class LLMConfig:
    endpoint_name: str
    escalation_endpoint_name: str
    host: str
    token: str


def get_llm_config() -> LLMConfig:
    """
    Returns the LLM endpoint configuration used for classification, RCA
    hypothesis generation, and recommendation steps.

    Uses Databricks Model Serving / AI Gateway by default so LLM calls
    are governed under the same Unity Catalog permissions and cost
    tracking as everything else in this codebase — check your
    workspace's Serving tab for the real, currently-configured endpoint
    names before relying on the defaults below.
    """
    db_cfg = get_databricks_config()
    return LLMConfig(
        endpoint_name=os.environ.get(
            "PIPELINE_REMEDIATION_LLM_ENDPOINT", "databricks-claude-haiku"
        ),
        escalation_endpoint_name=os.environ.get(
            "PIPELINE_REMEDIATION_ESCALATION_ENDPOINT", "databricks-claude-sonnet"
        ),
        host=db_cfg.host,
        token=db_cfg.token,
    )


def call_llm(prompt: str, use_escalation_model: bool = False, max_tokens: int = 1024) -> str:
    """
    Single entry point every module in src/ uses to call an LLM. Keeps
    model routing (cheap-first, escalate-on-low-confidence) in one
    place rather than scattered per-module — mirrors the cost-tiering
    rule in SKILL.md as actual, enforced code.
    """
    cfg = get_llm_config()
    endpoint = cfg.escalation_endpoint_name if use_escalation_model else cfg.endpoint_name

    resp = requests.post(
        f"{cfg.host}/serving-endpoints/{endpoint}/invocations",
        headers={"Authorization": f"Bearer {cfg.token}"},
        json={"messages": [{"role": "user", "content": prompt}], "max_tokens": max_tokens},
        timeout=60,
    )
    if resp.status_code != 200:
        raise QueryFailedError(
            f"LLM call to {endpoint} failed: {resp.status_code} {resp.text[:300]}"
        )
    data = resp.json()
    return data["choices"][0]["message"]["content"]
