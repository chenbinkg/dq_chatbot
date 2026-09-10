"""
Strands `@tool` functions exposed to the Collibra DQ chatbot agent.

Design notes
------------
- Every *write* action (create/update a DatasetDef) is split into a `propose_*`
  tool (read-only, returns a diff + short-lived `change_id`) and an `apply_*`
  tool that only executes once the human has seen the diff and asked to
  proceed. The system prompt (agent_instruction.txt) instructs the agent to
  always show the diff and wait for explicit user confirmation before calling
  `apply_*`.
- Pending changes are cached in-memory (per-process) with a TTL so a stale
  change_id can't be replayed much later or reused across unrelated turns.
- Two regions are supported (apac / cn) matching the existing dq_automation
  Collibra CDQ deployments; credentials are read from environment variables
  (see .env.example) so this runs standalone, without Databricks secrets.
"""

from __future__ import annotations

import copy
import difflib
import json
import os
import re
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from strands import tool

from collibra_dq_client import CollibraDQClient
import redshift_connections
import bu_mapping_reference
import dataset_builder
import business_unit
import chat_store
from jira_logger import (
    CHANGE_REQUEST_PROJECT_KEY,
    close_issue_with_path,
    find_change_task_key,
    jira_get_issue,
    log_jira_change_request,
    merge_description_with_investigation,
)

_CHANGE_TTL_SECONDS = 15 * 60
_pending_changes: dict[str, dict[str, Any]] = {}

_clients: dict[str, CollibraDQClient] = {}

# Maps DatasetDef top-level fields we track in change history to the
# change_item label used in public.dqm_chatbot_change_history.
DATASET_DEF_FIELD_LABELS = {
    "metaTags": "Meta_tag",
    "scheduleTime": "Schedule_time",
    "linkId": "Link_id",
    "jobDescription": "Job_description",
}

# Maps custom-rule fields we track in change history to the change_item label.
RULE_FIELD_LABELS = {
    "ruleNm": "Rule_name",
    "ruleValue": "Rule_value",
    "ruleType": "Rule_type",
    "columnName": "Column_name",
    "businessCategory": "Business_category",
    "businessDesc": "Business_description",
    "dimId": "Dimension_id",
    "dimName": "Dimension_name",
    "isActive": "Is_active",
    "points": "Points",
    "perc": "Perc",
    "filterQuery": "Filter_query",
    "tolerance": "Tolerance",
    "purpose": "Purpose",
    "suppressed": "Suppressed",
}

# Fixed metaTags[1] (Data Domain) taxonomy -- one value per dataset.
DATA_DOMAINS = [
    "Commercial-(Other)",
    "Commercial-Omnichannel",
    "Commercial-SFE",
    "Market-Access/Pricing",
    "Master-Data",
    "Medical",
    "Patient",
]

# Fixed metaTags[2] (Sub-Domain) codes -- a dataset can carry multiple, comma-separated
# (e.g. "PRO,HCO,HCP").
SUBDOMAIN_CODES = {
    "ACT": "Activity (Call Detail, Email, Newsletter)",
    "EMP": "Employee",
    "FIN": "Finance",
    "GEO": "Geography (e.g. Korea)",
    "HCO": "Healthcare Organization / Customer (e.g. Tokyo University Hospital)",
    "HCP": "Healthcare Professional (e.g. Doc Brown)",
    "ORG": "Organization (IPN Business Unit, MAF, CE, NPP Department)",
    "OTH": "Other (Population Statistics, Covid Data)",
    "PAT": "Patient (Treatment Duration)",
    "PRO": "Product (Brand, Product Codes, TA, DA)",
    "SAL": "Sales (Sales Amount)",
}


def _get_client(region: str = "apac") -> CollibraDQClient:
    region = (region or "apac").strip().lower()
    if region not in _clients:
        base_url = os.getenv(f"CDQ_BASE_URL_{region.upper()}")
        username = os.getenv(f"CDQ_USERNAME_{region.upper()}")
        password = os.getenv(f"CDQ_PASSWORD_{region.upper()}")
        _clients[region] = CollibraDQClient(base_url=base_url, username=username, password=password, region=region)
    return _clients[region]


def _stash_change(action: str, region: str, dataset: str, payload: dict[str, Any], diff: str) -> str:
    change_id = uuid.uuid4().hex[:8]
    _pending_changes[change_id] = {
        "action": action,
        "region": region,
        "dataset": dataset,
        "payload": payload,
        "diff": diff,
        "created_at": time.time(),
    }
    return change_id


def _pop_valid_change(change_id: str) -> dict[str, Any]:
    change = _pending_changes.pop(change_id, None)
    if not change:
        raise ValueError(f"No pending change found for change_id '{change_id}'. Propose the change again.")
    if time.time() - change["created_at"] > _CHANGE_TTL_SECONDS:
        raise ValueError(f"change_id '{change_id}' expired ({_CHANGE_TTL_SECONDS // 60} min TTL). Propose the change again.")
    return change


def _diff_summary(before: dict[str, Any], after: dict[str, Any]) -> str:
    keys = sorted(set(before) | set(after))
    lines = []
    for key in keys:
        old, new = before.get(key), after.get(key)
        if old != new:
            lines.append(f"  - {key}: {old!r} -> {new!r}")
    return "\n".join(lines) if lines else "  (no field-level changes detected)"


def _find_pending_update(region: str, dataset: str) -> Optional[str]:
    """Return the change_id of an unexpired pending 'update' proposal for this dataset, if any."""
    now = time.time()
    for cid, change in _pending_changes.items():
        if (
            change["action"] == "update"
            and change["region"] == region
            and change["dataset"] == dataset
            and now - change["created_at"] <= _CHANGE_TTL_SECONDS
        ):
            return cid
    return None


def _propose_update(region: str, dataset: str, patch: dict[str, Any]) -> dict[str, Any]:
    """Merge `patch` (top-level DatasetDef fields) into any pending 'update' change for this
    dataset instead of stashing a separate one, so that a chain of propose_dataset_update /
    propose_profile_settings_update calls followed by a single apply_dataset_change results
    in exactly one PUT + one run_job, not one per proposal.
    """
    client = _get_client(region)
    existing_id = _find_pending_update(region, dataset)

    if existing_id:
        change = _pending_changes[existing_id]
        base_before = change["base_before"]
        base_payload = change["payload"]
    else:
        base_before = client.get_dataset_def(dataset) or {"dataset": dataset}
        base_payload = copy.deepcopy(base_before)

    merged = copy.deepcopy(base_payload)
    merged.update(patch)
    diff = _diff_summary(base_before, merged)

    if existing_id:
        change["payload"] = merged
        change["diff"] = diff
        change["created_at"] = time.time()  # slide the TTL forward on each merged proposal
        change_id = existing_id
    else:
        change_id = _stash_change("update", region, dataset, merged, diff)
        _pending_changes[change_id]["base_before"] = base_before
        _pending_changes[change_id]["run_date"] = base_before.get("runId")

    return {"change_id": change_id, "diff": diff, "merged": merged, "base_payload": base_payload}


def _stringify(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, (list, dict)):
        return json.dumps(value, default=str)
    return str(value)


def _dataset_def_entries(before: dict[str, Any], after: dict[str, Any]) -> list[tuple[str, str, Any, Any]]:
    """Diff the DatasetDef fields + profile settings that change history tracks."""
    entries: list[tuple[str, str, Any, Any]] = []
    for key, label in DATASET_DEF_FIELD_LABELS.items():
        old, new = before.get(key), after.get(key)
        if old != new:
            entries.append(("Dataset definitions", label, old, new))

    old_profile = before.get("profile") or {}
    new_profile = after.get("profile") or {}
    for key in dataset_builder.PROFILE_MODIFIABLE_FLAGS:
        old_v, new_v = old_profile.get(key), new_profile.get(key)
        if old_v != new_v:
            entries.append(("Profile setting", key, old_v, new_v))
    return entries


def _rule_entries(rule_nm: str, before: dict[str, Any], after: dict[str, Any]) -> list[tuple[str, str, Any, Any]]:
    """Diff a custom rule's tracked fields, one entry per field that actually changed
    (e.g. only log ruleValue when it changed, not on every dimName/purpose edit)."""
    entries: list[tuple[str, str, Any, Any]] = []
    for key, label in RULE_FIELD_LABELS.items():
        old, new = before.get(key), after.get(key)
        if old != new:
            entries.append(("Custom rule", f"{rule_nm}.{label}", old, new))
    return entries


_RULE_NAME_PATTERN = re.compile(r"if_[a-z0-9]+(?:_[a-z0-9]+)*")


def _validate_new_rule_best_practices(rule_payload: dict[str, Any]) -> list[str]:
    """Best-practice checks for a brand-new custom rule. Never applied to an existing rule,
    including one being renamed via propose_rule_change(old_rule_nm=...) -- that is treated
    as an update to the rule it replaces, not a brand-new rule."""
    issues: list[str] = []
    rule_nm = str(rule_payload.get("ruleNm") or "")
    column_name = rule_payload.get("columnName")
    rule_repo = rule_payload.get("ruleRepo") or ""
    rule_type = rule_payload.get("ruleType") or ""

    if not column_name:
        issues.append(
            "columnName is not set -- if this rule targets specific column(s) of the source "
            'table, register one of them (e.g. "columnName": "Id").'
        )

    if not _RULE_NAME_PATTERN.fullmatch(rule_nm):
        issues.append(
            f"ruleNm '{rule_nm}' does not follow the naming convention -- it should start with "
            "'if_{columnName}_...', be succinct, and use lowercase words joined by underscores "
            "(e.g. 'if_product_description_is_tr_jnj_check_null_empty')."
        )

    if rule_repo:
        if rule_type != "CUSTOM":
            issues.append(f"ruleType should be 'CUSTOM' when ruleRepo (a template rule) is set, got '{rule_type}'.")
        if column_name and rule_payload.get("ruleValue") != column_name:
            issues.append("ruleValue should equal columnName when using a template rule (ruleRepo set).")
        if column_name:
            expected_nm = f"if_{column_name}_is_{rule_repo}"
            if rule_nm != expected_nm:
                issues.append(f"For this template rule, the expected ruleNm is '{expected_nm}'.")
    elif rule_type not in ("SQLF", "SQLG"):
        issues.append("ruleType should be 'SQLF' or 'SQLG' when no ruleRepo (template rule) is used.")

    return issues


_CHANGE_LOG_SEPARATOR = "~" * 72  # visually splits stacked change-history entries; distinct
# from jira_logger's own "-"*72 marker so merge_description_with_investigation never treats
# it as the boundary to strip.
_MAX_DIFF_LINES = 60


def _highlight_diff(before: Optional[str], after: Optional[str]) -> str:
    """Render a compact unified diff so a change buried in a long ruleValue/SQL block is
    easy to spot, instead of dumping the full before/after text twice."""
    before_lines = (before or "").splitlines()
    after_lines = (after or "").splitlines()
    diff = list(difflib.unified_diff(before_lines, after_lines, lineterm=""))
    if not diff:
        return "  (no textual difference detected)"
    if len(diff) > _MAX_DIFF_LINES:
        diff = diff[:_MAX_DIFF_LINES] + ["... (diff truncated)"]
    return "\n".join(f"  {line}" for line in diff)


def _format_jira_change_description(
    entries: list[tuple[str, str, Any, Any]],
    change_reason: str,
    change_by: str,
) -> str:
    """Build the Jira description body for one batch of applied changes (the dataset name
    is already the ticket summary, so it isn't repeated here)."""
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    lines = [
        f"- Changed by: {change_by}",
        f"- Change date: {timestamp}",
        f"- Reason: {change_reason or 'N/A'}",
        "",
    ]
    for category, item, before, after in entries:
        if item.endswith(".Rule_value"):
            lines.append(f"- [{category}] {item} (diff):")
            lines.append(_highlight_diff(_stringify(before), _stringify(after)))
        else:
            lines.append(f"- [{category}] {item}: {_stringify(before)} -> {_stringify(after)}")
    return "\n".join(lines)


# Queued (not yet synced to Jira) change-history batches per dataset, keyed by
# (region, dataset). Flushed by the sync_jira_change_request tool -- once per dataset per
# conversation is enough, instead of round-tripping to Jira on every single apply.
_pending_jira_batches: dict[tuple[str, str], list[dict[str, Any]]] = {}


def _queue_jira_entries(
    region: str,
    dataset: str,
    entries: list[tuple[str, str, Any, Any]],
    change_reason: str,
    change_by: str,
) -> bool:
    """Queue a batch of changed entries for a later Jira sync. Returns True if anything
    was queued."""
    if not entries:
        return False
    _pending_jira_batches.setdefault((region, dataset), []).append(
        {"entries": entries, "change_reason": change_reason, "change_by": change_by}
    )
    return True


def _format_jira_batches(batches: list[dict[str, Any]]) -> str:
    """Format multiple queued change batches (each with its own changed-by/reason/date)
    into one combined Jira description block."""
    return "\n\n".join(
        _format_jira_change_description(batch["entries"], batch["change_reason"], batch["change_by"])
        for batch in batches
    )


def _log_change_entries(
    dataset: str,
    region: str,
    dataset_category: str,
    entries: list[tuple[str, str, Any, Any]],
    change_reason: str,
) -> dict[str, Any]:
    """Log a batch of change-history rows for one dataset, one row per changed field, into
    the database immediately, and queue the same entries for a later Jira sync (see the
    sync_jira_change_request tool) -- Jira is not written to on every apply, only when the
    agent explicitly syncs after all of a dataset's changes are done. Returns a dict with
    whether the database write succeeded and whether anything was queued for Jira."""
    result = {"db_logged": False, "jira_pending": False}
    if not entries:
        return result
    ctx = chat_store.get_session_context()
    change_by = ctx.get("app_user") or "unknown"
    rows = [
        {
            "dataset": dataset,
            "region": region,
            "session_id": ctx.get("session_id") or None,
            "dataset_category": dataset_category,
            "change_category": category,
            "change_item": item,
            "change_from": _stringify(before),
            "change_to": _stringify(after),
            "change_reason": change_reason or None,
            "change_by": change_by,
        }
        for category, item, before, after in entries
    ]
    try:
        chat_store.log_changes(rows)
        result["db_logged"] = True
    except Exception as exc:
        print(f"Failed to log change history for dataset '{dataset}': {exc}")

    result["jira_pending"] = _queue_jira_entries(region, dataset, entries, change_reason, change_by)
    return result


def _find_pending_rule_change(region: str, dataset: str) -> Optional[str]:
    """Return the change_id of an unexpired pending 'upsert_rule' change for this dataset, if any."""
    now = time.time()
    for cid, change in _pending_changes.items():
        if (
            change["action"] == "upsert_rule"
            and change["region"] == region
            and change["dataset"] == dataset
            and now - change["created_at"] <= _CHANGE_TTL_SECONDS
        ):
            return cid
    return None


# ----------------------------------------------------------------------
# Read-only tools
# ----------------------------------------------------------------------
@tool
def list_datasets(region: str = "apac", name_contains: str = "") -> str:
    """List Collibra DQ dataset names for a region, optionally filtered by substring.

    Args:
        region: "apac" or "cn".
        name_contains: Optional case-insensitive substring filter on dataset name.
    """
    datasets = _get_client(region).list_datasets()
    if name_contains:
        needle = name_contains.lower()
        datasets = [d for d in datasets if needle in d.lower()]
    return "\n".join(datasets) if datasets else "No datasets found."


@tool
def get_dataset_definition(dataset: str, region: str = "apac") -> dict[str, Any]:
    """Fetch the current DatasetDef (metaTags, scheduleTime, load config, etc.) for a dataset.

    Args:
        dataset: Exact Collibra DQ dataset name (e.g. "ds_conn_s3_angen_maf_target_ki_au").
        region: "apac" or "cn".
    """
    return _get_client(region).get_dataset_def(dataset)

@tool
def get_dataset_alert(dataset: str, region: str = "apac") -> dict[str, Any]:
    """Fetch the current alert configuration for a dataset.

    Args:
        dataset: Exact Collibra DQ dataset name (e.g. "ds_conn_s3_angen_maf_target_ki_au").
        region: "apac" or "cn".
    """
    return _get_client(region).get_alert_dataset(dataset)


@tool
def get_business_unit(dataset: str, region: str = "apac") -> dict[str, Any]:
    """Resolve the business unit (Market / Project / CDE flag) currently mapped to a dataset.

    Args:
        dataset: Exact Collibra DQ dataset name.
        region: "apac" or "cn".
    """
    result = _get_client(region).get_business_unit_for_dataset(dataset)
    return result or {"dataset": dataset, "business_unit": None, "message": "No business unit mapping found."}


@tool
def suggest_metatags(dataset: str, region: str = "apac") -> dict[str, Any]:
    """Suggest metaTags (Data Domain / Sub-Domain) for a dataset based on its current
    definition, naming convention, business unit mapping, and (if the dataset's
    connection/db/table are known) a Redshift column sample. Does NOT write anything --
    use propose_dataset_update to apply a chosen suggestion.

    Recommended workflow before calling this:
    1. list_redshift_connections to find the right connectionName for the dataset.
    2. sample_table_data to pull real column names + sample rows for that table.
    3. find_similar_tagged_datasets to see how comparable datasets are already tagged
       in public.dqm_business_unit_mapping -- prefer reusing an existing Data Domain
       that similar datasets use rather than picking one in isolation.
    4. list_available_metatags to cross-check against Collibra's live tag catalog.
    Then use the fixed taxonomy returned here (data_domain_options / subdomain_codes)
    to pick metaTags[1] (exactly one Data Domain) and metaTags[2] (one or more
    comma-separated Sub-Domain codes, e.g. "PRO,HCO,HCP") -- do not invent values
    outside these lists.

    Args:
        dataset: Exact Collibra DQ dataset name.
        region: "apac" or "cn".
    """
    client = _get_client(region)
    current_def = client.get_dataset_def(dataset) or {}
    bu = client.get_business_unit_for_dataset(dataset) or {}
    current_tags = current_def.get("metaTags") or []
    return {
        "dataset": dataset,
        "current_metaTags": current_tags,
        "business_unit": bu.get("business_unit"),
        "market": bu.get("market"),
        "project": bu.get("project"),
        "data_domain_options": DATA_DOMAINS,
        "subdomain_codes": SUBDOMAIN_CODES,
        "note": (
            "By convention metaTags[1] is the Data Domain (single value from "
            "data_domain_options) and metaTags[2] is the Sub-Domain (comma-separated "
            "codes from subdomain_codes's keys), refer to available current_metaTags. "
            "Ground your suggestion in the dataset's real columns/sample data via "
            "sample_table_data when possible, state your reasoning to the user, and let "
            "them confirm the exact tags before calling propose_dataset_update."
        ),
    }


@tool
def list_redshift_connections(name_contains: str = "") -> list[dict[str, str]]:
    """List known Collibra DQ source connections (name, type, location), optionally
    filtered by substring on connectionName. Use this to find the right
    `connectionName` for a dataset before sampling its table or creating a DatasetDef.
    This is a maintained static registry (no Collibra API lists connections).

    Args:
        name_contains: Optional case-insensitive substring filter on connectionName.
    """
    return redshift_connections.list_connections(name_contains)


@tool
def sample_table_data(conn_name: str, db_mn: str, table_mn: str, limit: int = 10) -> dict[str, Any]:
    """Pull column names and up to `limit` sample rows from a Redshift table, to help
    ground metaTag/business-domain suggestions in real data. Read-only (SELECT ... LIMIT).
    The Redshift host is resolved from conn_name ("local" -> JP local cluster, "region" ->
    regional cluster) -- use list_redshift_connections first to find a valid conn_name.

    Args:
        conn_name: Collibra connectionName (must contain "local" or "region").
        db_mn: Schema/database name in Redshift.
        table_mn: Table name in Redshift.
        limit: Max rows to sample (capped at 50).
    """
    return redshift_connections.sample_table(conn_name, db_mn, table_mn, limit)


@tool
def search_redshift_tables(keyword: str, cluster: str = "", db_mn: str = "", limit: int = 50) -> dict[str, Any]:
    """Search Redshift for table names containing `keyword` (case-insensitive), across both
    clusters unless `cluster` is given. Read-only. Use this to find the physical schema.table
    backing a cross-reference (e.g. for a custom rule) when you only know a rough keyword and
    not the exact table name -- then check if it already has a matching CDQ dataset (via
    suggest_dataset_name/list_datasets) before assuming one needs to be created.

    Args:
        keyword: Case-insensitive substring to match against table names (e.g. "account_plan").
        cluster: Optional "local" or "region" to restrict the search to one cluster.
        db_mn: Optional exact schema name to restrict the search to.
        limit: Max rows to return per cluster (capped at 200).
    """
    return redshift_connections.search_tables(keyword, cluster=cluster or None, db_mn=db_mn or None, limit=limit)


@tool
def search_redshift_columns(
    keyword: str,
    cluster: str = "",
    db_mn: str = "",
    table_mn: str = "",
    limit: int = 50,
) -> dict[str, Any]:
    """Search Redshift for column names containing `keyword`, optionally scoped to a schema
    and/or table, across both clusters unless `cluster` is given. Read-only. Use this to
    resolve the exact join/id column name (e.g. "source_id" vs "account_id") when
    constructing a custom rule that cross-references another table.

    Args:
        keyword: Case-insensitive substring to match against column names (e.g. "source_id").
        cluster: Optional "local" or "region" to restrict the search to one cluster.
        db_mn: Optional exact schema name to restrict the search to.
        table_mn: Optional exact table name to restrict the search to.
        limit: Max rows to return per cluster (capped at 200).
    """
    return redshift_connections.search_columns(
        keyword, cluster=cluster or None, db_mn=db_mn or None, table_mn=table_mn or None, limit=limit
    )


@tool
def test_redshift_query(cluster: str, query: str, limit: int = 20) -> dict[str, Any]:
    """Run a single read-only SELECT query against a Redshift cluster (wrapped and capped at
    `limit` rows) to troubleshoot a candidate custom-rule query before saving it -- e.g.
    sample the rows a rule's WHERE clause would flag as breaks. Collibra's `@dataset_name`
    tokens are not valid raw SQL -- substitute the real `schema.table` for each one first.
    Only works when the referenced tables are in Redshift (not S3-backed datasets).

    Args:
        cluster: "local" or "region" -- the cluster the tables live on.
        query: A single SELECT statement (no semicolons/multiple statements).
        limit: Max rows to return (capped at 100).
    """
    return redshift_connections.run_readonly_query(cluster, query, limit)


@tool
def list_s3_objects(prefix_path: str, max_keys: int = 50) -> dict[str, Any]:
    """List object keys under an s3:// bucket/prefix (folder), read-only. Use this to find
    the exact file name when you know the bucket/folder but not the precise file -- e.g.
    "s3://itx-adj-anz-refined/itx-adj-anz-prod/Extracts/dq_iconnect_source/" -- before
    calling sample_s3_file or suggest_dataset_name.

    Args:
        prefix_path: s3:// bucket + folder prefix to list under.
        max_keys: Max object keys to return (capped at 200).
    """
    return redshift_connections.list_s3_objects(prefix_path, max_keys)


@tool
def sample_s3_file(s3_path: str, limit: int = 10) -> dict[str, Any]:
    """Read the header + up to `limit` sample rows from a CSV file in S3, to ground
    metaTag/business-domain suggestions and linkId choices in real data when building an
    s3-backed dataset -- the s3 equivalent of sample_table_data. Read-only (a ranged GET
    capped at ~256KB, so large files aren't fully downloaded).

    Args:
        s3_path: Full s3:// path to the CSV file (e.g.
            "s3://itx-adj-anz-refined/.../Account_Plan_vod__c.csv").
        limit: Max sample rows to return (capped at 50).
    """
    return redshift_connections.sample_s3_file(s3_path, limit)


@tool
def list_available_metatags(region: str = "apac") -> list[dict[str, Any]]:
    """Fetch Collibra DQ's live metaTag catalog (GET /v2/metatag) to cross-check proposed
    Data Domain / Sub-Domain values against what Collibra actually has configured.

    Args:
        region: "apac" or "cn".
    """
    return _get_client(region).list_metatags()


@tool
def find_similar_tagged_datasets(
    dataset: str,
    conn_name: str = "",
    db_mn: str = "",
    table_mn: str = "",
    top_n: int = 10,
) -> dict[str, Any]:
    """Find already-tagged datasets in public.dqm_business_unit_mapping that most resemble
    a given dataset, and summarise which "Data Domain" and subDomain values they use.
    Use this to ground a metaTags[1] (Data Domain) suggestion in how comparable datasets
    are already tagged, instead of guessing from the dataset name alone. Read-only.

    Args:
        dataset: The new/target Collibra DQ dataset name.
        conn_name: Optional connectionName -- an exact match boosts similarity.
        db_mn: Optional Redshift schema/database name -- an exact match boosts similarity.
        table_mn: Optional table name, used as extra name tokens for matching.
        top_n: How many similar datasets to return (capped at 50).
    """
    return bu_mapping_reference.find_similar_datasets(
        dataset=dataset,
        conn_name=conn_name or None,
        db_mn=db_mn or None,
        table_mn=table_mn or None,
        top_n=top_n,
    )


@tool
def data_domain_distribution() -> dict[str, int]:
    """Count how many existing datasets carry each "Data Domain" value in
    public.dqm_business_unit_mapping. Useful sanity check on which domains are
    actually in active use. Read-only.
    """
    return bu_mapping_reference.data_domain_distribution()


def _normalize_name_part(value: str) -> str:
    # Only "-" becomes "_"; existing underscores are preserved (e.g. "vod__c").
    return re.sub(r"[^a-z0-9_]+", "_", (value or "").lower().replace("-", "_"))


def _existing_dataset_names(region: str) -> set[str]:
    raw = _get_client(region).list_datasets() or []
    names = set()
    for item in raw:
        name = item.get("dataset") if isinstance(item, dict) else item
        if name:
            names.add(str(name).lower())
    return names


@tool
def suggest_dataset_name(
    source_type: str,
    db_mn: str = "",
    table_mn: str = "",
    s3_path: str = "",
    region: str = "apac",
) -> dict[str, Any]:
    """Derive a Collibra DQ dataset name following the naming convention
    ds_<system>_<cluster>_<schema>_<table>, and check it against existing datasets.

    For source_type="redshift": provide db_mn and table_mn. Both Redshift clusters are
    probed to determine whether the table lives on the "local" or "region" cluster, giving
    e.g. ds_redshift_region_myschema_mytable.

    For source_type="s3": provide s3_path (e.g.
    "s3://itx-adj-anz-refined/.../dq_iconnect_source/Coaching_Report_vod__c.csv"). The
    system is "conn_s3" (no cluster segment), the connectionName is resolved from the
    bucket/prefix, the schema is the immediate parent folder and the table is the file
    name without its extension, giving e.g.
    ds_conn_s3_dq_iconnect_source_coaching_report_vod__c.

    All "-" are converted to "_". If the name already exists, numbered alternatives are
    returned since one schema.table can back multiple datasets.

    Args:
        source_type: "redshift" or "s3".
        db_mn: Redshift schema name (redshift only).
        table_mn: Redshift table name (redshift only).
        s3_path: Full s3:// path to the source file (s3 only).
        region: Collibra region to check the name against ("apac" or "cn").
    """
    source_type = (source_type or "").strip().lower()
    details: dict[str, Any] = {}

    if source_type == "redshift":
        if not db_mn or not table_mn:
            raise ValueError("db_mn and table_mn are required for source_type='redshift'.")
        probe = redshift_connections.detect_redshift_cluster(db_mn, table_mn)
        details["cluster_probe"] = probe
        cluster = probe.get("cluster")
        if not cluster:
            matched = probe.get("matched_clusters") or []
            hint = (
                f"table found on multiple clusters {matched}; ask the user which one to use"
                if matched
                else "table not found on either cluster; check the schema/table name and Redshift credentials"
            )
            return {"base_name": None, "message": f"Could not determine the cluster: {hint}.", **details}
        parts = ["ds", "redshift", cluster, _normalize_name_part(db_mn), _normalize_name_part(table_mn)]

    elif source_type == "s3":
        if not s3_path:
            raise ValueError("s3_path is required for source_type='s3'.")
        parsed = redshift_connections.parse_s3_path(s3_path)
        details["s3_parsed"] = parsed
        parts = ["ds", "conn_s3", _normalize_name_part(parsed["schema"]), _normalize_name_part(parsed["table"])]

    else:
        raise ValueError(f"Unsupported source_type '{source_type}'. Use 'redshift' or 's3'.")

    base_name = "_".join(p for p in parts if p)
    existing = _existing_dataset_names(region)

    suggestions = [base_name] if base_name.lower() not in existing else []
    suffix = 2
    while len(suggestions) < 3 and suffix < 20:
        candidate = f"{base_name}_{suffix}"
        if candidate.lower() not in existing:
            suggestions.append(candidate)
        suffix += 1

    return {
        "base_name": base_name,
        "base_name_exists": base_name.lower() in existing,
        "suggested_names": suggestions,
        "region": region,
        **details,
    }


@tool
def check_link_id_uniqueness(cluster: str, db_mn: str, table_mn: str, link_id: list[str]) -> dict[str, Any]:
    """Test whether a candidate linkId (primary key) uniquely identifies rows in a Redshift
    table, returning up to 10 duplicated key combinations. Read-only. Every new dataset must
    have a linkId configured -- if the proposed columns are not unique, ask the user to add
    more columns (up to 10 categorical columns) and re-test.

    Args:
        cluster: "local" or "region" (as returned by suggest_dataset_name).
        db_mn: Redshift schema name.
        table_mn: Redshift table name.
        link_id: Candidate key columns.
    """
    return redshift_connections.check_link_id_uniqueness(cluster, db_mn, table_mn, link_id)


@tool
def propose_new_dq_dataset(
    source_type: str,
    dataset: str,
    connection_name: str,
    schedule_time: str,
    data_domain: str,
    sub_domain: str,
    link_id: list[str],
    db_mn: str = "",
    table_mn: str = "",
    s3_path: str = "",
    job_description: str = "",
    region: str = "apac",
) -> dict[str, Any]:
    """Preview creating a fully configured new DQ dataset that follows all best practices:
    naming convention, Data Domain + subDomain metaTags, MON-FRI DAILY schedule in
    Asia/Singapore, standard spark sizing, only row/null/empty behaviour checks enabled,
    shape/outliers/patterns turned off, dupe enabled when a linkId is given (redshift only),
    plus the standard "Low Dataset Score" email alert and an initial job run.

    This does NOT write anything -- it returns a summary, any best-practice violations, and
    a change_id. Show it to the user and only call apply_dataset_change after they confirm.

    Gather these from the user first: schedule_time, linkId (verified with
    check_link_id_uniqueness), and the Data Domain / subDomain (see suggest_metatags and
    find_similar_tagged_datasets). Use suggest_dataset_name for `dataset` and
    list_redshift_connections / suggest_dataset_name for `connection_name`.

    Args:
        source_type: "redshift" or "s3".
        dataset: New dataset name from suggest_dataset_name.
        connection_name: Collibra connectionName backing the source.
        schedule_time: Daily run time, e.g. "05:00:00".
        data_domain: One value from suggest_metatags' data_domain_options.
        sub_domain: One or more comma-separated codes, e.g. "PRO,HCO".
        link_id: Primary key / linkId columns (required).
        db_mn: Redshift schema (redshift only).
        table_mn: Redshift table (redshift only).
        s3_path: Source s3:// file path (s3 only).
        job_description: Optional description.
        region: "apac" or "cn".
    """
    source_type = (source_type or "").strip().lower()
    template_name = dataset_builder.REDSHIFT_TEMPLATE if source_type == "redshift" else dataset_builder.S3_TEMPLATE

    client = _get_client(region)
    template = client.get_dataset_def(template_name)
    if not template:
        raise ValueError(f"Could not load reference template dataset '{template_name}'.")

    payload = dataset_builder.build_dataset_def(
        template=template,
        dataset=dataset,
        source_type=source_type,
        connection_name=connection_name,
        schedule_time=schedule_time,
        data_domain=data_domain,
        sub_domain=sub_domain,
        link_id=link_id,
        db_mn=db_mn,
        table_mn=table_mn,
        s3_path=s3_path,
        job_description=job_description,
    )
    issues = dataset_builder.validate_best_practices(payload, source_type)
    alert_payload = dataset_builder.build_alert_payload(dataset)

    summary = {
        "dataset": payload["dataset"],
        "source_type": source_type,
        "connectionName": payload["load"]["connectionName"],
        "query_or_path": payload["load"]["query"] or payload["load"]["filePath"],
        "metaTags": payload["metaTags"],
        "linkId": payload["linkId"],
        "dupe_on": payload["dupe"]["on"],
        "jobSchedule": payload["jobSchedule"],
        "spark": {k: payload["spark"][k] for k in ("numExecutors", "driverMemory", "driverCores", "executorMemory", "executorCores", "conf")},
        "shape_enabled": payload["shape"]["enabled"],
        "alert": alert_payload,
        "run_date": payload["runId"],
    }

    change_id = _stash_change("create_full", region, dataset, payload, "")
    _pending_changes[change_id]["alert_payload"] = alert_payload
    _pending_changes[change_id]["run_date"] = payload["runId"]

    return {
        "change_id": change_id,
        "region": region,
        "summary": summary,
        "best_practice_issues": issues,
        "message": (
            "Review this with the user. Calling apply_dataset_change(change_id) will create "
            "the DatasetDef, configure the Low Dataset Score alert, and trigger the first job run."
        ),
    }


@tool
def list_business_units(region: str = "apac") -> dict[str, Any]:
    """List all Collibra business units definition, not related to datasets, 
    split into markets (subId=null), project rows ("<market> - <project>", 
    subId=parent market id) and the distinct project names in
    use. Read-only.

    Args:
        region: "apac" or "cn".
    """
    units = _get_client(region).list_business_units().get("result", [])
    split = business_unit.split_business_units(units)
    return {
        "markets": sorted(split["markets"]),
        "project_names": split["project_names"],
        "business_units": units,
    }


@tool
def suggest_business_unit(dataset: str, db_mn: str = "", table_mn: str = "", region: str = "apac") -> dict[str, Any]:
    """Suggest the "<market> - <project>" business unit for a new or untagged dataset,
    inferred from its schema and name, and report whether that business unit already
    exists in Collibra. Read-only.

    Market rules: schema "pixonomy" -> REGION, schema "na"/"kr" -> KR, otherwise the
    market token found in the schema/dataset name (ANZ, CN, HK, JP, KR, REGION, TW, VN).
    Project rules: "angen_maf" -> "ANGen MAF", "angen" -> "ANGen", "_dp_" ->
    "Data-Product", otherwise "E.AI".

    Always present available_projects to the user and let them confirm or override the
    suggested project before calling propose_business_unit_assignment. If needs_creation
    is true, applying the change will create the new business unit first.

    Returns following in dictionary form:
        {
            "dataset",
            "suggested_market",
            "suggested_project",
            "suggested_business_unit",
            "existing_business_unit",
            "needs_creation",
            "market_id",
            "available_markets",
            "available_projects",
            "projects_in_market",
        }

    Args:
        dataset: Collibra DQ dataset name.
        db_mn: Source schema name, used for the market rules.
        table_mn: Source table name, used as extra inference tokens.
        region: "apac" or "cn".
    """
    units = _get_client(region).list_business_units().get("result", [])
    result = business_unit.suggest(units, dataset=dataset, db_mn=db_mn, table_mn=table_mn)
    result["region"] = region
    return result


@tool
def propose_business_unit_assignment(
    dataset: str,
    market: str,
    project: str,
    region: str = "apac",
) -> dict[str, Any]:
    """Preview attaching a business unit to a dataset. This does NOT write anything -- it
    returns a change_id. Show it to the user and only call apply_dataset_change after they
    explicitly confirm. If "<market> - <project>" does not exist yet, applying will create
    the business unit under the market first, then attach it to the dataset.

    Args:
        dataset: Collibra DQ dataset name.
        market: One of ANZ, CN, HK, JP, KR, REGION, TW, VN.
        project: Project name, e.g. "ANGen MAF", "Data-Product", "E.AI".
        region: "apac" or "cn".
    """
    market = (market or "").strip()
    project = (project or "").strip()
    if not market or not project:
        raise ValueError("Both market and project are required.")

    units = _get_client(region).list_business_units().get("result", [])
    split = business_unit.split_business_units(units)
    market_bu = split["markets"].get(market)
    if not market_bu:
        raise ValueError(f"Unknown market '{market}'. Available: {sorted(split['markets'])}")

    bu_name = f"{market} - {project}"
    # existing bu name available
    existing = next((bu for bu in split["projects"] if bu.get("name") == bu_name), None)
    # existing bu assigned to dataset (if any)
    existing_bu_payload = _get_client(region).get_business_unit_id_by_dataset(dataset) or {}
    # current business unit name attached to the dataset, for change-history logging
    before_bu_name = (_get_client(region).get_business_unit_for_dataset(dataset) or {}).get("business_unit")

    change_id = _stash_change("assign_bu", region, dataset, existing_bu_payload, "")
    _pending_changes[change_id].update(
        {"business_unit_name": bu_name, "market_id": market_bu["id"], "existing_bu": existing, "before_bu_name": before_bu_name}
    )

    return {
        "change_id": change_id,
        "dataset": dataset,
        "region": region,
        "business_unit": bu_name,
        "will_create_business_unit": existing is None,
        "business_unit_id": existing.get("id") if existing else None,
        "message": "Review with the user. Call apply_dataset_change(change_id) only after they confirm.",
    }


@tool
def get_dq_findings(dataset: str, run_date: str, region: str = "apac") -> dict[str, Any]:
    """Fetch DQ job findings (rule breaks, outliers, scores) for a dataset run.

    Args:
        dataset: Exact Collibra DQ dataset name.
        run_date: Run date/id as returned by the dataset's job history (e.g. "2026-08-20").
        region: "apac" or "cn".
    """
    return _get_client(region).get_findings(dataset, run_date)


@tool
def list_rules(region: str = "apac") -> list[dict[str, Any]]:
    """List all active DQ rule definitions for a region.

    Args:
        region: "apac" or "cn".
    """
    return _get_client(region).list_rules()


@tool
def get_dataset_rules(dataset: str, region: str = "apac") -> list[dict[str, Any]]:
    """Fetch the custom/rule definitions currently configured for one dataset
    (GET /v3/rules/{dataset}). Read-only.

    Args:
        dataset: Exact Collibra DQ dataset name.
        region: "apac" or "cn".
    """
    return _get_client(region).get_rules_for_dataset(dataset)


@tool
def validate_rule_run(dataset: str, rule_nm: str, run_date: str, region: str = "apac") -> dict[str, Any]:
    """Check whether one custom rule passed on a specific job run (GET
    /v3/jobs/{dataset}/{run_date}/findings, then looks up rule_nm in the "rules" list).
    Call this after apply_dataset_change on a propose_rule_change whose ruleValue changed
    (requires_run_validation=True) -- give the triggered job time to finish first, since
    findings for a run that hasn't completed yet won't include this rule. Not needed for a
    rename-only change or edits to dimension/description/purpose/other metadata, since
    those don't change what the rule evaluates.

    Args:
        dataset: Exact Collibra DQ dataset name.
        rule_nm: The rule's current ruleNm (its new name, if this followed a rename) to look
            up in the run's findings.
        run_date: The run_date the job was triggered with -- e.g. apply_dataset_change's
            "run_date" field, or the dataset's latest runId from get_dataset_definition.
        region: "apac" or "cn".
    """
    findings = _get_client(region).get_findings(dataset, run_date) or {}
    rules = findings.get("rules") or []
    match = next((r for r in rules if r.get("ruleNm") == rule_nm), None)
    if match is None:
        return {
            "dataset": dataset,
            "rule_nm": rule_nm,
            "run_date": run_date,
            "found": False,
            "message": (
                "This rule has no findings yet for this run -- the job may still be running "
                "or hasn't started. Wait a bit and try again."
            ),
        }
    passed = str(match.get("breakMsg", "")).strip().upper() == "PASSING" and not match.get("exception")
    return {
        "dataset": dataset,
        "rule_nm": rule_nm,
        "run_date": run_date,
        "found": True,
        "passed": passed,
        "score": match.get("score"),
        "perc": match.get("perc"),
        "breakMsg": match.get("breakMsg"),
        "exception": match.get("exception"),
        "rows": findings.get("rows"),
        "passFail": findings.get("passFail"),
    }


@tool
def list_template_rules(region: str = "apac") -> list[dict[str, Any]]:
    """List Collibra DQ's template rule catalog (GET /v2/templateRules), e.g.
    "tr_jnj_check_null_empty" or "tr_jnj_hco_bed_count". Each entry has ruleName (use as
    ruleRepo), ruleValue (a "$colNm"-templated expression describing the underlying check),
    ruleTyp, ruleDescription, dimId, etc. Read-only. Use this to find a suitable template
    before building a ruleType="CUSTOM" rule via propose_rule_change.

    Args:
        region: "apac" or "cn".
    """
    return _get_client(region).list_template_rules()


# ----------------------------------------------------------------------
# Write tools: propose (preview) then apply (confirm)
# ----------------------------------------------------------------------
@tool
def propose_dataset_update(
    dataset: str,
    meta_tags: Optional[list[str]] = None,
    schedule_time: Optional[str] = None,
    job_description: Optional[str] = None,
    link_id: Optional[list[str]] = None,
    region: str = "apac",
) -> dict[str, Any]:
    """Preview an update to a dataset's definition (metaTags, scheduleTime, jobDescription,
    linkId, etc.
    ). This does NOT write anything -- it returns a diff and a change_id.
    Show the diff to the user and only call apply_dataset_change after they explicitly
    confirm.

    If another update is already pending for the same dataset (e.g. from
    propose_profile_settings_update), this merges into that same change_id rather than
    creating a second one, so a single apply_dataset_change still only triggers one job run.

    Args:
        dataset: Exact Collibra DQ dataset name.
        meta_tags: New full metaTags list, if changing (e.g. ["ds", "SalesForce", "Orders"]).
        schedule_time: New cron/schedule string, if changing.
        job_description: New job description, if changing.
        region: "apac" or "cn".
    """
    patch: dict[str, Any] = {}
    if meta_tags is not None:
        patch["metaTags"] = meta_tags
    if schedule_time is not None:
        patch["scheduleTime"] = schedule_time
    if job_description is not None:
        patch["jobDescription"] = job_description
    if link_id is not None:
        patch["linkId"] = link_id
    if not patch:
        raise ValueError("No fields provided to update.")

    result = _propose_update(region, dataset, patch)
    return {
        "change_id": result["change_id"],
        "dataset": dataset,
        "region": region,
        "diff": result["diff"],
        "message": "Review the diff with the user. Call apply_dataset_change(change_id) only after they confirm.",
    }


@tool
def propose_profile_settings_update(
    dataset: str,
    behaviorRowCheck: Optional[bool] = None,
    behaviorNullCheck: Optional[bool] = None,
    behaviorEmptyCheck: Optional[bool] = None,
    behaviorTimeCheck: Optional[bool] = None,
    behaviorMinValueCheck: Optional[bool] = None,
    behaviorMaxValueCheck: Optional[bool] = None,
    behaviorMeanValueCheck: Optional[bool] = None,
    behaviorUniqueCheck: Optional[bool] = None,
    profileStringLength: Optional[bool] = None,
    region: str = "apac",
) -> dict[str, Any]:
    """Preview enabling/disabling a dataset's profile checks. This does NOT write anything --
    it returns a diff and a change_id. Show the diff to the user and only call
    apply_dataset_change after they explicitly confirm.

    Only these profile settings can be changed: behaviorRowCheck, behaviorNullCheck,
    behaviorEmptyCheck, behaviorTimeCheck, behaviorMinValueCheck, behaviorMaxValueCheck,
    behaviorMeanValueCheck, behaviorUniqueCheck, profileStringLength. detectStringNumerics,
    detectTopnBotn, detectScalePrecision and behaviorShiftCheck are fixed defaults enforced
    by the Collibra UI (they cannot be unselected there either) -- do not attempt to change
    them; this tool raises an error if asked to.

    Pass only the settings the user wants to change; omitted (None) settings are left as-is.

    If another update is already pending for the same dataset (e.g. from
    propose_dataset_update), this merges into that same change_id rather than creating a
    second one, so a single apply_dataset_change still only triggers one job run.

    Args:
        dataset: Exact Collibra DQ dataset name.
        behaviorRowCheck: Enable/disable the row count check.
        behaviorNullCheck: Enable/disable the null check.
        behaviorEmptyCheck: Enable/disable the empty-value check.
        behaviorTimeCheck: Enable/disable the time/date behaviour check.
        behaviorMinValueCheck: Enable/disable the min-value check.
        behaviorMaxValueCheck: Enable/disable the max-value check.
        behaviorMeanValueCheck: Enable/disable the mean-value check.
        behaviorUniqueCheck: Enable/disable the uniqueness check.
        profileStringLength: Enable/disable string-length profiling.
        region: "apac" or "cn".
    """
    requested = {
        "behaviorRowCheck": behaviorRowCheck,
        "behaviorNullCheck": behaviorNullCheck,
        "behaviorEmptyCheck": behaviorEmptyCheck,
        "behaviorTimeCheck": behaviorTimeCheck,
        "behaviorMinValueCheck": behaviorMinValueCheck,
        "behaviorMaxValueCheck": behaviorMaxValueCheck,
        "behaviorMeanValueCheck": behaviorMeanValueCheck,
        "behaviorUniqueCheck": behaviorUniqueCheck,
        "profileStringLength": profileStringLength,
    }
    settings = {k: v for k, v in requested.items() if v is not None}
    if not settings:
        raise ValueError("No profile settings provided to update.")

    existing_id = _find_pending_update(region, dataset)
    if existing_id:
        current_profile = _pending_changes[existing_id]["payload"].get("profile") or {}
    else:
        current_profile = (_get_client(region).get_dataset_def(dataset) or {}).get("profile") or {}
    updated_profile = dataset_builder.build_profile_settings_patch(current_profile, settings)
    profile_diff = _diff_summary(current_profile, updated_profile)

    result = _propose_update(region, dataset, {"profile": updated_profile})
    return {
        "change_id": result["change_id"],
        "dataset": dataset,
        "region": region,
        "diff": profile_diff,
        "message": "Review the diff with the user. Call apply_dataset_change(change_id) only after they confirm.",
    }


@tool
def propose_rule_change(
    dataset: str,
    rule_payload: dict[str, Any],
    old_rule_nm: Optional[str] = None,
    region: str = "apac",
) -> dict[str, Any]:
    """Preview creating a new custom DQ rule, updating an existing one, or renaming one, for
    a dataset (POST /v3/rules -- Collibra uses the same POST endpoint for both create and
    update, matched by dataset+ruleNm). This does NOT write anything -- it returns a diff, a
    change_id, and (for brand-new rules only) any best_practice_issues. Show it to the user
    and only call apply_dataset_change after they explicitly confirm.

    rule_payload must include "ruleNm" (the rule's name after this change). To rename an
    existing rule, pass its current name as old_rule_nm and the new name in
    rule_payload["ruleNm"] -- there is no rename endpoint, so applying this deletes the old
    rule and creates a new one under the new name (DELETE + POST /v3/rules), then still
    triggers one job run. Leave old_rule_nm unset to create a brand-new rule or update an
    existing one in place (matched against get_dataset_rules by rule_payload["ruleNm"]).
    Any other rule fields you omit are kept from the existing rule when updating/renaming;
    "dataset" is set automatically. See the sample schema returned by get_dataset_rules for
    the full set of fields (ruleType, ruleValue, columnName, businessCategory, businessDesc,
    dimId, dimName, isActive, points, perc, filterQuery, tolerance, purpose, suppressed,
    etc.).

    Best practices enforced for NEW rules only (never for updates or renames of an existing
    rule):
    - columnName: if the rule targets specific column(s) of the source table, set one of
      them here (e.g. "columnName": "Id").
    - ruleNm: intuitive, descriptive, not too long, lowercase, words joined by underscores, 
    can reflect the rule purpose, starting with "if_{columnName}_...".
    - Three ruleType/ruleRepo/ruleValue combinations:
      1. Template rule -- ruleType="CUSTOM", ruleRepo=<template ruleName from
         list_template_rules, e.g. "tr_jnj_check_null_empty">, ruleValue=columnName,
         ruleNm="if_{columnName}_is_{ruleRepo}" (e.g.
         "if_product_description_is_tr_jnj_check_null_empty").
      2. Full custom SQL across dataset(s) -- ruleType="SQLF", ruleRepo="", ruleValue is a
         full SELECT using "@dataset_name" tokens (e.g.
         "SELECT * FROM @ds_x WHERE ...").
      3. Simple condition on the current dataset -- ruleType="SQLG", ruleRepo="", ruleValue
         is a bare condition (e.g. "market <> 'kr'").
    Use list_template_rules first to find a suitable template (ruleRepo) before building a
    ruleType="CUSTOM" rule.

    If this change alters ruleValue (whether or not it's also a rename), the returned dict
    sets requires_run_validation=True -- after apply_dataset_change, wait for the triggered
    job to finish and call validate_rule_run(dataset, ruleNm, run_date) to confirm the rule
    actually passes before telling the user the change is done. Renaming a rule or editing
    only its dimension/description/purpose/other metadata does not change what gets
    evaluated, so no such validation is needed for those -- just note the job takes some
    time to run.

    A custom rule is not part of the DatasetDef, so this proposal is tracked separately
    from propose_dataset_update/propose_profile_settings_update -- but applying it still
    triggers a job run so the rule takes effect for the next validation. If another rule
    change is already pending for this dataset, it is bundled into the same change_id so
    one apply_dataset_change still only triggers a single job run.

    Args:
        dataset: Exact Collibra DQ dataset name the rule belongs to.
        rule_payload: Rule fields to create/update/rename; must include "ruleNm" (the name
            after this change).
        old_rule_nm: Set this to the rule's current ruleNm only when renaming it (i.e. when
            rule_payload["ruleNm"] differs from the rule's current name). Leave unset
            otherwise.
        region: "apac" or "cn".
    """
    rule_nm = (rule_payload or {}).get("ruleNm")
    if not rule_nm:
        raise ValueError("rule_payload must include 'ruleNm'.")

    client = _get_client(region)
    existing_rules = client.get_rules_for_dataset(dataset) or []
    lookup_nm = old_rule_nm or rule_nm
    existing_rule = next((r for r in existing_rules if r.get("ruleNm") == lookup_nm), None)
    if old_rule_nm and not existing_rule:
        raise ValueError(f"No existing rule named '{old_rule_nm}' found on dataset '{dataset}' to rename.")
    is_rename = bool(old_rule_nm and old_rule_nm != rule_nm)
    if is_rename and any(r.get("ruleNm") == rule_nm for r in existing_rules):
        raise ValueError(f"A rule named '{rule_nm}' already exists on dataset '{dataset}' -- choose a different name.")

    merged_rule = {**(existing_rule or {}), **rule_payload, "dataset": dataset}
    diff = _diff_summary(existing_rule or {}, merged_rule)
    is_update = existing_rule is not None
    best_practice_issues = [] if is_update else _validate_new_rule_best_practices(merged_rule)
    requires_validation = is_update and (existing_rule or {}).get("ruleValue") != merged_rule.get("ruleValue")

    entry = {
        "rule": merged_rule,
        "is_update": is_update,
        "before": existing_rule,
        "delete_rule_nm": old_rule_nm if is_rename else None,
        "requires_validation": requires_validation,
    }

    existing_id = _find_pending_rule_change(region, dataset)
    if existing_id:
        change = _pending_changes[existing_id]
        rules = [
            r
            for r in change["payload"]["rules"]
            if r["rule"].get("ruleNm") != rule_nm and r.get("delete_rule_nm") != old_rule_nm
        ]
        rules.append(entry)
        change["payload"]["rules"] = rules
        change["diff"] = diff
        change["created_at"] = time.time()
        change_id = existing_id
    else:
        current_def = client.get_dataset_def(dataset) or {}
        change_id = _stash_change("upsert_rule", region, dataset, {"rules": [entry]}, diff)
        _pending_changes[change_id]["run_date"] = current_def.get("runId")

    message = "Review the diff with the user. Call apply_dataset_change(change_id) only after they confirm."
    if is_rename:
        message += f" This renames '{old_rule_nm}' to '{rule_nm}' (deletes the old rule, creates the new one)."
    if requires_validation:
        message += (
            " ruleValue changed -- after applying, wait for the triggered job to finish, "
            "then call validate_rule_run(dataset, ruleNm, run_date) to confirm the rule "
            "passes before telling the user the change is done."
        )

    return {
        "change_id": change_id,
        "dataset": dataset,
        "region": region,
        "will_create_new_rule": not is_update,
        "will_rename_from": old_rule_nm if is_rename else None,
        "requires_run_validation": requires_validation,
        "diff": diff,
        "best_practice_issues": best_practice_issues,
        "message": message,
    }


@tool
def apply_dataset_change(change_id: str, change_reason: str) -> dict[str, Any]:
    """Execute a previously proposed dataset create/update after the user has confirmed
    it in chat. Only call this after showing the diff and receiving explicit confirmation.
    Every applied change is written to the change-history audit table immediately, and
    also queued for Jira -- the returned dict includes "change_logged_to_database" (bool)
    and "jira_change_pending" (bool, True if there's now something queued for Jira for
    this dataset). This does NOT write to Jira itself: once the user is done making changes
    to this dataset in the conversation, confirm with them and call
    sync_jira_change_request(dataset, region, market) once to flush everything queued into
    a single Jira ticket update.

    Args:
        change_id: The change_id returned by propose_dataset_update or propose_new_dq_dataset.
        change_reason: Mandatory short reason for the change (from the user), stored in the
            audit trail alongside who/what/when. Ask the user for it before calling this
            tool -- do not invent one and do not call this tool without it.
    """
    if not change_reason or not change_reason.strip():
        raise ValueError(
            "change_reason is required to apply a change. Ask the user for a brief reason "
            "and call apply_dataset_change again with it -- the change_id is still valid."
        )
    change = _pop_valid_change(change_id)
    client = _get_client(change["region"])
    region = change["region"]
    dataset = change["dataset"]
    outcome = {"status": "not applied", "action": change["action"], "dataset": dataset}

    if change["action"] == "assign_bu":
        bu = change["existing_bu"]
        created = None
        if not bu:
            created = client.create_business_unit(change["business_unit_name"], sub_id=change["market_id"])
            bu = created.get("id")
            # bu = client.get_business_unit_by_name(change["business_unit_name"]).get("result")
        bu_id = bu.get("id") if isinstance(bu, dict) else None
        if bu_id is None:
            raise ValueError(f"Could not resolve an id for business unit '{change['business_unit_name']}'.")
        existing_bu_payload = change.get("payload") or {}
        if not existing_bu_payload:
            # no business unit defined
            result = client.assign_business_unit_to_dataset(bu_id, dataset)
        else:
            # update existing business unit mapping to the new business unit
            result = client.update_business_unit_to_dataset(
                id=existing_bu_payload.get("id"), 
                business_unit_id=bu_id, 
                dataset=dataset
                )

        log_result = _log_change_entries(
            dataset,
            region,
            "Existing",
            [("Business unit", "business_unit", change.get("before_bu_name"), change["business_unit_name"])],
            change_reason,
        )
        return {
            "status": "applied",
            "action": "assign_bu",
            "dataset": dataset,
            "business_unit": change["business_unit_name"],
            "business_unit_id": bu_id,
            "created_business_unit": created,
            "result": result,
            "change_logged_to_database": log_result["db_logged"],
            "jira_change_pending": log_result["jira_pending"],
        }
    elif change["action"] == "assign_alert":
        result = client.create_alert(change["alert_payload"])
        existing_alert = change.get("existing_alert") or {}
        log_result = _log_change_entries(
            dataset,
            region,
            "Existing",
            [(
                "Email alert",
                "low dataset score",
                existing_alert.get("alertFormatValue"),
                change["alert_payload"].get("alertFormatValue"),
            )],
            change_reason,
        )
        return {
            "status": "applied",
            "action": "assign_alert",
            "dataset": dataset,
            "alert_email": change.get("alert_email"),
            "result": result,
            "change_logged_to_database": log_result["db_logged"],
            "jira_change_pending": log_result["jira_pending"],
        }

    elif change["action"] == "update":
        result = client.update_dataset_def(dataset, change["payload"])
        outcome: dict[str, Any] = {
                "status": "applied",
                "action": "update",
                "dataset": dataset,
                "dataset_def": result,
            }
        try:
            outcome["job_run"] = client.run_job(dataset, change["run_date"])
            print(f"Triggered initial job run for dataset '{dataset}' with runId '{change['run_date']}'.")
        except Exception as exc:
            outcome["job_run_error"] = str(exc)
            print(f"Failed to trigger initial job run for dataset '{dataset}': {exc}")
        log_result = _log_change_entries(
            dataset, 
            region, 
            "Existing", 
            _dataset_def_entries(change["base_before"], change["payload"]), 
            change_reason
        )
        outcome["change_logged_to_database"] = log_result["db_logged"]
        outcome["jira_change_pending"] = log_result["jira_pending"]
        return outcome

    # A full best-practice create also needs the standard alert and an initial run.
    elif change["action"] == "create_full":
        result = client.create_dataset_def(change["payload"])
        outcome: dict[str, Any] = {
            "status": "applied",
            "action": change["action"],
            "dataset": dataset,
            "dataset_def": result,
        }
        try:
            outcome["alert"] = client.create_alert(change["alert_payload"])
        except Exception as exc:
            outcome["alert_error"] = str(exc)
        try:
            outcome["job_run"] = client.run_job(dataset, change["run_date"])
            print(f"Triggered initial job run for dataset '{dataset}' with runId '{change['run_date']}'.")
        except Exception as exc:
            outcome["job_run_error"] = str(exc)
            print(f"Failed to trigger initial job run for dataset '{dataset}': {exc}")
        entries = _dataset_def_entries({}, change["payload"])
        entries.append(("Email alert", "low dataset score", None, change["alert_payload"].get("alertFormatValue")))
        log_result = _log_change_entries(dataset, region, "New", entries, change_reason)
        outcome["change_logged_to_database"] = log_result["db_logged"]
        outcome["jira_change_pending"] = log_result["jira_pending"]
        return outcome

    # Custom rules are written via /v3/rules, not the DatasetDef, but still need a job
    # run so the rule change takes effect for the next validation.
    elif change["action"] == "upsert_rule":
        results = []
        deleted_rules = []
        for entry in change["payload"]["rules"]:
            # Collibra has no separate PUT for rules; POST handles both create and update.
            results.append(client.create_rule(entry["rule"]))
            delete_nm = entry.get("delete_rule_nm")
            if delete_nm:
                # Renaming: no rename endpoint, so the old rule is deleted once the new
                # one is created, and a job run below lets the new rule take effect.
                try:
                    client.delete_rule(dataset, delete_nm)
                    deleted_rules.append(delete_nm)
                except Exception as exc:
                    outcome.setdefault("delete_rule_errors", {})[delete_nm] = str(exc)
        outcome = {
            **outcome,
            "status": "applied",
            "action": "upsert_rule",
            "dataset": dataset,
            "rules": results,
            "deleted_rules": deleted_rules,
        }
        try:
            outcome["job_run"] = client.run_job(dataset, change["run_date"])
            print(f"Triggered job run for dataset '{dataset}' with runId '{change['run_date']}'.")
        except Exception as exc:
            outcome["job_run_error"] = str(exc)
            print(f"Failed to trigger job run for dataset '{dataset}': {exc}")
        entries = []
        rules_requiring_validation = []
        for entry in change["payload"]["rules"]:
            rule_nm = entry["rule"].get("ruleNm")
            entries.extend(_rule_entries(rule_nm, entry.get("before") or {}, entry["rule"]))
            if entry.get("requires_validation"):
                rules_requiring_validation.append(rule_nm)
        outcome["run_date"] = change["run_date"]
        outcome["rules_requiring_validation"] = rules_requiring_validation
        if rules_requiring_validation:
            outcome["message"] = (
                "ruleValue changed for "
                f"{', '.join(rules_requiring_validation)} -- wait for the triggered job to "
                "finish, then call validate_rule_run(dataset, ruleNm, run_date) for each to "
                "confirm they pass before telling the user the change is done."
            )
        log_result = _log_change_entries(dataset, region, "Existing", entries, change_reason)
        outcome["change_logged_to_database"] = log_result["db_logged"]
        outcome["jira_change_pending"] = log_result["jira_pending"]
        return outcome

    return outcome


@tool
def sync_jira_change_request(dataset: str, region: str, market: str) -> dict[str, Any]:
    """Push all pending (queued but not-yet-synced) change-history entries for this dataset
    into its Jira "DQ Change Request" ticket, stacked on top of anything already there, then
    close the ticket. Database logging from apply_dataset_change already happened
    immediately and independently of this. Call this tool once after the user is done
    making changes to this dataset in the conversation (not after every single
    apply_dataset_change) -- confirm with the user first, since this is the step that
    actually writes to Jira.

    Args:
        dataset: Collibra DQ dataset name.
        region: "apac" or "cn".
        market: The dataset's market, e.g. "ANZ", "CN", "HK", "JP", "KR", "REGION", "TW",
            "VN" -- see suggest_business_unit's suggested_market for this dataset. This
            Jira project tracks which market a dataset belongs to via the ticket's
            version/fixVersion (there is no per-dataset version), so this is required to
            attach the ticket to the right market.
    """
    key = (region, dataset)
    batches = _pending_jira_batches.pop(key, [])
    if not batches:
        return {
            "dataset": dataset,
            "region": region,
            "synced": False,
            "message": "No pending changes queued for this dataset -- nothing to log to Jira.",
        }

    new_description = _format_jira_batches(batches)
    change_by = batches[-1]["change_by"]
    try:
        existing_key = find_change_task_key(CHANGE_REQUEST_PROJECT_KEY, dataset)
        if existing_key:
            original_issue = jira_get_issue(existing_key, fields="description")
            original_description = (original_issue.get("fields") or {}).get("description") or ""
            if original_description.strip():
                new_description = f"{new_description}\n\n{_CHANGE_LOG_SEPARATOR}"
            new_description = merge_description_with_investigation(original_description, new_description)
        issue_key = log_jira_change_request(dataset, new_description, change_by, market=market)
        close_issue_with_path(issue_key)
    except Exception as exc:
        _pending_jira_batches[key] = batches  # keep queued so the agent/user can retry
        raise RuntimeError(f"Failed to log Jira change request for dataset '{dataset}': {exc}") from exc

    return {
        "dataset": dataset,
        "region": region,
        "market": market,
        "synced": True,
        "jira_issue_key": issue_key,
        "batches_synced": len(batches),
    }


@tool
def propose_email_alert(dataset: str, region: str = "apac") -> dict[str, Any]:
    """Preview setting/changing the alert email for a dataset. This does NOT write anything --
    it returns a diff and a change_id. Show the diff to the user and only call
    apply_dataset_change after they explicitly confirm.

    Args:
        dataset: Exact Collibra DQ dataset name.
        # alert_email: Email address (or comma-separated addresses) to receive DQ alerts.
        region: "apac" or "cn".
    """
    existing_alert = _get_client(region).get_alert_dataset(dataset)
    if not existing_alert:
        alert_payload = dataset_builder.build_alert_payload(dataset)
    else:
        alert_payload = existing_alert.copy()

    return propose_alert_update(dataset=dataset, alert_payload=alert_payload, region=region)

@tool
def propose_alert_update(
    dataset: str,
    alert_payload: dict[str, Any],
    region: str = "apac",
) -> dict[str, Any]:
    """Preview setting/changing the alert for a dataset. This does NOT write anything --
    it returns a change_id. Show it to the user and only call apply_dataset_change after they
    explicitly confirm.

    Args:
        dataset: Collibra DQ dataset name.
        alert_payload: Alert configuration payload.
        region: "apac" or "cn".
    """

    existing_alert = _get_client(region).get_alert_dataset(dataset)
    if alert_payload not in existing_alert:
        change_id = _stash_change("assign_alert", region, dataset, {}, "")
        _pending_changes[change_id].update(
            {"alert_payload": alert_payload, "existing_alert": existing_alert}
        )
    else:
        change_id = None

    return {
        "change_id": change_id,
        "dataset": dataset,
        "region": region,
        "alert_payload": alert_payload,
        "existing_alert": existing_alert,
        "message": "Review with the user. Call apply_dataset_change(change_id) only after they confirm.",
    }

ALL_TOOLS = [
    apply_dataset_change,
    check_link_id_uniqueness,
    data_domain_distribution,
    find_similar_tagged_datasets,
    get_business_unit,
    get_dataset_alert,
    get_dataset_definition,
    get_dataset_rules,
    get_dq_findings,
    list_available_metatags,
    list_business_units,
    list_datasets,
    list_redshift_connections,
    list_rules,
    list_s3_objects,
    list_template_rules,
    propose_alert_update,
    propose_business_unit_assignment,
    propose_dataset_update,
    propose_email_alert,
    propose_new_dq_dataset,
    propose_profile_settings_update,
    propose_rule_change,
    sample_s3_file,
    sample_table_data,
    search_redshift_columns,
    search_redshift_tables,
    suggest_business_unit,
    suggest_dataset_name,
    suggest_metatags,
    sync_jira_change_request,
    test_redshift_query,
    validate_rule_run,
]
