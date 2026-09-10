"""
Builds best-practice Collibra DQ DatasetDef payloads for new datasets.

Rather than assembling the (very large) DatasetDef schema from scratch, this
clones a known-good reference dataset via GET /v3/datasetDefs/{dataset} and
overrides the fields that differ, then enforces the agreed best-practice
settings (schedule, spark sizing, which profile/layer checks are on, min, mean 
and max are off for adaptive rule settings, shape not on for columns containing highly mixed or freeform data.
dupe driven by linkId or primary keys, alert config, business unit config). null metaTags[0] is okay.
DRAFT runState is okay. Note that shape, outliers, pattern, dupes belong to layer rule settings.
Adaptive rule settings contain row count, execution time, uniqueness, null values, empty fields,
min, mean, max, data type check and schema change check.


Reference templates (override with env vars if they are ever retired):
  redshift -> ds_redshift_region_anz_itg_enrichment_brand_ta_mapping
  s3       -> ds_conn_s3_dq_iconnect_source_Account_Objectives_vs_iDiscover
"""

from __future__ import annotations

import copy
import os
from datetime import date
from typing import Any, Optional

REDSHIFT_TEMPLATE = os.getenv("CDQ_REDSHIFT_TEMPLATE", "ds_redshift_region_anz_itg_enrichment_brand_ta_mapping")
S3_TEMPLATE = os.getenv("CDQ_S3_TEMPLATE", "ds_conn_s3_dq_iconnect_source_Account_Objectives_vs_iDiscover")

CDQ_HOST = "collibradq-prod-postgres.cufto4yp2h96.ap-southeast-1.rds.amazonaws.com/idiscover"
CDQ_USER = "SA_JAC_DQ_PROD_SVER"

SCHEDULE_DAYS = ["FRI", "TUE", "MON", "WED", "THU"]
SCHEDULE_TIMEZONE = "Asia/Singapore"
SCHEDULE_FREQUENCY = "DAILY"

SPARK_DEFAULTS = {
    "numExecutors": 1,
    "driverMemory": "2g",
    "driverCores": 2,
    "executorMemory": "1g",
    "executorCores": 2,
}
SPARK_CONF_REDSHIFT = "spark.cores.max=2"
SPARK_CONF_S3 = "spark.hadoop.fs.s3a.endpoint.region=ap-southeast-1,spark.cores.max=2"

REDSHIFT_LOAD = {
    "lib": "/opt/collibra/owlbase/owl/drivers/redshift",
    "additionalLib": "/opt/collibra/owlbase/owl/drivers/postgres/",
    "driverName": "com.amazon.redshift.jdbc42.Driver",
}
S3_LOAD = {
    "fileQuery": "select * from dataset",
    "lib": "remoteFileConndriverlocation",
    "additionalLib": "/opt/collibra/owlbase/owl/drivers/redshift/",
    "driverName": "remoteFileConnDriver",
}

# Profile flags: only these behaviour checks stay on, everything else is forced off.
PROFILE_ON = [
    "behaviorRowCheck",
    "behaviorNullCheck",
    "behaviorEmptyCheck",
    "detectStringNumerics",
    "detectTopnBotn",
    "detectScalePrecision",
    "behaviorShiftCheck",
]
PROFILE_OFF = [
    "behaviorTimeCheck",
    "behaviorMinValueCheck",
    "behaviorMaxValueCheck",
    "behaviorMeanValueCheck",
    "behaviorUniqueCheck",
    "profileStringLength",
]

# These profile flags are fixed defaults in the Collibra UI (cannot be unselected there
# either), so they must never be changed by propose_profile_settings_update.
PROFILE_LOCKED_FLAGS = [
    "detectStringNumerics",
    "detectTopnBotn",
    "detectScalePrecision",
    "behaviorShiftCheck",
]

# Every other profile check flag is user-modifiable per dataset.
PROFILE_MODIFIABLE_FLAGS = [
    "behaviorRowCheck",
    "behaviorNullCheck",
    "behaviorEmptyCheck",
    "behaviorTimeCheck",
    "behaviorMinValueCheck",
    "behaviorMaxValueCheck",
    "behaviorMeanValueCheck",
    "behaviorUniqueCheck",
    "profileStringLength",
]


def build_profile_settings_patch(current_profile: dict[str, Any], settings: dict[str, bool]) -> dict[str, Any]:
    """Merge requested on/off `settings` onto `current_profile`, rejecting locked/unknown flags."""
    locked_requested = sorted(set(settings) & set(PROFILE_LOCKED_FLAGS))
    if locked_requested:
        raise ValueError(
            f"These profile settings are fixed defaults in Collibra and cannot be changed: {locked_requested}"
        )
    unknown = sorted(set(settings) - set(PROFILE_MODIFIABLE_FLAGS))
    if unknown:
        raise ValueError(
            f"Unknown/unsupported profile settings: {unknown}. Modifiable settings are: {PROFILE_MODIFIABLE_FLAGS}"
        )
    updated = copy.deepcopy(current_profile) if current_profile else {}
    updated.update(settings)
    return updated

ALERT_NAME = "Low Dataset Score"
ALERT_CONDITION = "score < 95"
ALERT_RECIPIENT = "DL-ASPAC-iDiscover-Data-Quality-Alert@its.jnj.com"


def build_alert_payload(dataset: str) -> dict[str, Any]:
    """Payload for POST /v3/alerts implementing the standard low-score email alert."""
    return {
        "dataset": dataset,
        "alertNm": ALERT_NAME,
        "alertCond": ALERT_CONDITION,
        "alertFormat": "EMAIL",
        "alertFormatValue": ALERT_RECIPIENT,
        "alertMsg": "",
        "batchName": "",
        "addRuleDetails": False,
        "active": True,
        "ruleName": "",
        "alertTypes": ["CONDITION"],
    }


def build_dataset_def(
    template: dict[str, Any],
    dataset: str,
    source_type: str,
    connection_name: str,
    schedule_time: str,
    data_domain: str,
    sub_domain: str,
    link_id: Optional[list[str]] = None,
    db_mn: str = "",
    table_mn: str = "",
    s3_path: str = "",
    run_date: Optional[str] = None,
    job_description: str = "",
    extra_meta_tag: Optional[str] = None,
) -> dict[str, Any]:
    """Clone `template` and apply the new dataset's identity plus best-practice settings."""
    source_type = source_type.strip().lower()
    if source_type not in {"redshift", "s3"}:
        raise ValueError(f"Unsupported source_type '{source_type}'. Use 'redshift' or 's3'.")

    payload = copy.deepcopy(template)
    run_date = run_date or date.today().isoformat()
    link_id = [c for c in (link_id or []) if c]

    payload["dataset"] = dataset
    payload["runId"] = run_date
    payload["host"] = CDQ_HOST
    payload["user"] = CDQ_USER
    payload["jobDescription"] = job_description
    payload["linkId"] = link_id or None
    payload.setdefault("pushdown", {})["dataset"] = dataset
    payload.setdefault("env", {})["dataset"] = dataset

    # metaTags convention: [<placeholder>, Data Domain, subDomain, <optional code>]
    meta_tags: list[Any] = [None, data_domain, sub_domain, None]
    if extra_meta_tag:
        meta_tags.append(extra_meta_tag)
    payload["metaTags"] = meta_tags

    load = payload.setdefault("load", {})
    load["connectionName"] = connection_name
    if source_type == "redshift":
        if not db_mn or not table_mn:
            raise ValueError("db_mn and table_mn are required for a redshift dataset.")
        load["query"] = f"select * from {db_mn}.{table_mn}"
        load["filePath"] = ""
        load["fileQuery"] = ""
        load["fullFile"] = False
        load.update(REDSHIFT_LOAD)
    else:
        if not s3_path:
            raise ValueError("s3_path is required for an s3 dataset.")
        load["query"] = ""
        # Collibra reads S3 through the Hadoop s3a:// scheme.
        load["filePath"] = s3_path.replace("s3://", "s3a://", 1)
        load["fullFile"] = True
        load.update(S3_LOAD)

    spark = payload.setdefault("spark", {})
    spark.update(SPARK_DEFAULTS)
    spark["conf"] = SPARK_CONF_REDSHIFT if source_type == "redshift" else SPARK_CONF_S3

    profile = payload.setdefault("profile", {})
    profile["on"] = True
    profile["shape"] = False
    for flag in PROFILE_ON:
        profile[flag] = True
    for flag in PROFILE_OFF:
        profile[flag] = False

    # Layer rules: shape / outliers / patterns off; dupe only for redshift with linkId.
    payload["outliers"] = []
    payload["patterns"] = []
    shape = payload.setdefault("shape", {})
    shape["enabled"] = False
    shape["columnSettings"] = []

    dupe = payload.setdefault("dupe", {})
    dupe_on = bool(link_id) and source_type == "redshift"
    dupe["on"] = dupe_on
    dupe["include"] = sorted(link_id) if dupe_on else None

    payload["jobSchedule"] = {
        "enabled": True,
        "dataset": dataset,
        "agentId": payload.get("agentId"),
        "runDateFormat": "yyyy-MM-dd",
        "scheduleFrequency": SCHEDULE_FREQUENCY,
        "scheduleTime": schedule_time,
        "timeZone": SCHEDULE_TIMEZONE,
        "scheduleDays": SCHEDULE_DAYS,
    }

    return payload


def validate_best_practices(payload: dict[str, Any], source_type: str) -> list[str]:
    """Return a list of best-practice violations; empty means the payload is compliant."""
    issues: list[str] = []
    dataset = payload.get("dataset") or ""

    if not dataset.startswith("ds_"):
        issues.append("dataset name does not follow the ds_<system>_<cluster>_<schema>_<table> convention")

    meta_tags = payload.get("metaTags") or []
    if len(meta_tags) < 2 or not meta_tags[1] or not meta_tags[2]:
        issues.append("metaTags must carry a Data Domain and a subDomain")

    schedule = payload.get("jobSchedule") or {}
    if not schedule.get("enabled"):
        issues.append("job schedule is not enabled")
    if schedule.get("timeZone") != SCHEDULE_TIMEZONE:
        issues.append(f"job schedule timeZone must be {SCHEDULE_TIMEZONE}")
    if schedule.get("scheduleFrequency") != SCHEDULE_FREQUENCY:
        issues.append(f"job schedule frequency must be {SCHEDULE_FREQUENCY}")
    if sorted(schedule.get("scheduleDays") or []) != sorted(SCHEDULE_DAYS):
        issues.append("job schedule days must be MON-FRI")
    if not schedule.get("scheduleTime"):
        issues.append("job schedule time is missing (ask the user)")

    load = payload.get("load", {})
    if (not payload.get("linkId")) and (not load.get("key")):
        issues.append("linkId / primary keys are not configured")

    if (payload.get("shape") or {}).get("enabled"):
        issues.append("shape layer must be turned off")
    if payload.get("outliers"):
        issues.append("outliers layer must be turned off")
    if payload.get("patterns"):
        issues.append("patterns layer must be turned off")

    if source_type == "s3" and (payload.get("dupe") or {}).get("on"):
        issues.append("dupe rule is only supported for redshift datasets")

    return issues
