"""
Low-level REST client for Collibra CDQ (DQ) used by the chatbot's agent tools.

Wraps `CollibraTokenManager` (../token_manager.py) with the specific endpoints
needed to read/write dataset definitions, resolve business units, and pull DQ
findings/rules. Mirrors the endpoint usage already proven out in
dq_automation/s1_query_dataset_and_bu.py and s2_query_dataset_details.py, but
adds create/update (POST/PUT) support for /v3/datasetDefs.

This module has no Gradio/strands dependency so it can be unit-tested or reused
on its own.
"""

from __future__ import annotations

from typing import Any, Dict, Mapping, List, Optional
import copy
import logging
import os
import sys
import requests
import urllib3

# token_manager.py lives one level up (shared with dq_automation's copy).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from token_manager import CollibraTokenManager

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

logger = logging.getLogger(__name__)

# profile drift threshold: for delta profile
DEFAULT_DRIFT_THRESHOLDS = {
    "none_upper": 1.0,
    "low_upper": 5.0,
    "medium_upper": 10.0,
}
# severity ranking for profile drift
SEVERITY_RANK = {
    "UNKNOWN": -1,
    "NONE": 0,
    "LOW": 1,
    "MEDIUM": 2,
    "HIGH": 3,
    "CRITICAL": 4,
}

class CollibraDQError(Exception):
    """Raised when a Collibra CDQ API call fails after retrying token refresh."""


class CollibraDQClient:
    """Thin REST wrapper around a single Collibra CDQ region (apac or cn)."""

    def __init__(self, base_url: str, username: str, password: str, region: str = "apac"):
        if not base_url or not username or not password:
            raise ValueError(f"Collibra CDQ credentials missing for region '{region}'")
        self.base_url = base_url.rstrip("/")
        self.region = region
        self.token_mgr = CollibraTokenManager(base_url=base_url, username=username, password=password, region=region)

    # ------------------------------------------------------------------
    # Core request helper (mirrors the safe_get retry-on-401 pattern used
    # in s1/s2, extended to support any HTTP method + JSON body).
    # ------------------------------------------------------------------
    def _request(self, method: str, path: str, max_retries: int = 1, **kwargs: Any) -> Any:
        url = f"{self.base_url}{path}"
        for attempt in range(max_retries + 1):
            headers = {**self.token_mgr.get_auth_header(), **kwargs.pop("headers", {})}
            response = requests.request(method, url, headers=headers, verify=self.token_mgr.verify_ssl, timeout=60, **kwargs)
            if response.status_code == 401 and attempt < max_retries:
                logger.warning("[%s] 401 from %s, forcing token refresh (attempt %d)", self.region, path, attempt + 1)
                self.token_mgr.get_token(force_refresh=True)
                continue
            if response.status_code >= 400:
                raise CollibraDQError(f"[{self.region}] {method} {path} failed: {response.status_code} {response.text[:500]}")
            if not response.content:
                return None
            return response.json()
        raise CollibraDQError(f"[{self.region}] {method} {path} failed after {max_retries} retries")

    # ------------------------------------------------------------------
    # Read: datasets / business units
    # ------------------------------------------------------------------
    def list_datasets(self) -> list[str]:
        data = self._request("GET", "/v2/getlistdatasets") or []
        return data

    def list_business_units(self) -> list[dict[str, Any]]:
        return self._request("GET", "/v2/business-unit") or []

    def list_business_unit_mappings(self) -> list[dict[str, Any]]:
        return self._request("GET", "/v2/business-unit-to-dataset") or []

    def get_business_unit_by_name(self, name: str) -> dict[str, Any]:
        """GET /v2/business-unit/getbyname to resolve a business unit (and its id) by name."""
        return self._request("GET", "/v2/business-unit/getbyname", params={"name": name})

    def get_business_unit_by_id(self, bu_id: str) -> dict[str, Any]:
        """GET /v2/business-unit to retrieve a business unit by id."""
        return self._request("GET", f"/v2/business-unit/{bu_id}").get("result", {})

    def get_business_unit_id_by_dataset(self, dataset: str) -> dict[str, Any]:
        """GET /v2/business-unit-to-dataset/getbydataset to retrieve a dataset's business unit id."""
        data = self._request("GET", "/v2/business-unit-to-dataset/getbydataset", params={"dataset": dataset})
        results = data.get("result", [])
        if results:
            result = results[0] # return the first bu definition
            return result
        return results

    def get_business_unit_for_dataset(self, dataset: str) -> Optional[dict[str, Any]]:
        """Resolve the business unit name (and parsed Market/Project/CDE) for one dataset."""
        result = self.get_business_unit_id_by_dataset(dataset) or {}
        bu_id = result.get("businessUnitId", None)
        id = result.get("id", None) # for put request to update the business unit mapping
        if bu_id:
            bu = self.get_business_unit_by_id(bu_id)
            bu_name = bu.get("name") if bu else None
            market, project, cde = self._parse_business_unit_name(bu_name)
            return {"dataset": dataset, "business_unit": bu_name, "market": market, "project": project, "cde": cde, "id": id}
        return None

    @staticmethod
    def _parse_business_unit_name(bu_name: Optional[str]) -> tuple[Optional[str], Optional[str], str]:
        """Parse "<Market> - <Project>" into (Market, Project, CDE) -- same convention as s1."""
        if not bu_name or not isinstance(bu_name, str):
            return None, None, "No"
        normalized = bu_name.strip()
        if " - " in normalized:
            market, project = (p.strip() for p in normalized.split(" - ", 1))
        else:
            market, project = normalized or None, None
        cde = "Yes" if isinstance(project, str) and project.upper() == "CDE" else "No"
        return market, project, cde

    # ------------------------------------------------------------------
    # Read: dataset definitions / findings / rules / profile delta / topN-bottomN
    # ------------------------------------------------------------------
    def get_dataset_def(self, dataset: str) -> dict[str, Any]:
        return self._request("GET", f"/v3/datasetDefs/{dataset}")

    def get_findings(self, dataset: str, run_date: str) -> dict[str, Any]:
        return self._request("GET", f"/v3/jobs/{dataset}/{run_date}/findings")

    def list_rules(self) -> list[dict[str, Any]]:
        return self._request("GET", "/v3/rules") or []

    def get_rules_for_dataset(self, dataset: str) -> list[dict[str, Any]]:
        """GET /v3/rules/{dataset} to list custom rule definitions for one dataset."""
        return self._request("GET", f"/v3/rules/{dataset}") or []

    def list_metatags(self) -> list[dict[str, Any]]:
        """Full catalog of valid Collibra DQ metaTags (GET /v2/metatag)."""
        return self._request("GET", "/v2/metatag") or []

    def list_template_rules(self) -> list[dict[str, Any]]:
        """Full catalog of Collibra DQ template rules (GET /v2/templateRules), e.g.
        "tr_jnj_check_null_empty" -- each has ruleName, ruleValue ($colNm placeholder),
        ruleTyp, ruleDescription, dimId, etc."""
        return self._request("GET", "/v2/templateRules") or []

    def get_profile_delta(self, dataset: str, run_date: str) -> list[dict[str, Any]]:
        """GET /v3/profile/deltas?dataset={dataset}&runId={run_date} to 
        retrieve the profile delta for a specific dataset run.
        The output JSON is a column-by-column profile drift response:
        - baseline says what the column historically looks like
        - datasetField says what the latest run observed
        - historicalSchema and currentSchema support schema drift detection
        - Top-level delta fields quantify the differences
        - validValuesRule carries an automatically generated range failure condition
        - changePercent summarises non-uniqueness profile differences
        - changePercentWithUniques appears to extend that score with uniqueness drift

        Args:
            dataset (str): The dataset identifier.
            run_date (str): The run date of the dataset.

        Returns:
            list[dict[str, Any]]: The profile delta as a list of dictionaries.
        """
        return self._request(
            "GET", 
            f"/v3/profile/deltas", 
            params={"dataset": dataset, "runId": run_date}
            )

    @staticmethod
    def safe_float(value: Any) -> Optional[float]:
        """Convert value to float when possible."""
        if value is None:
            return None

        if isinstance(value, (int, float)):
            return float(value)

        if isinstance(value, str):
            value = value.strip()

            if value in ("", "---"):
                return None

            try:
                return float(value)
            except ValueError:
                return None

        return None

    @staticmethod
    def normalize_profile_delta_record(record: Dict[str, Any]) -> Dict[str, Any]:
        """
        Normalize a single Collibra profile delta record.
        """

        baseline = record.get("baseline", {})
        dataset_field = record.get("datasetField", {})
        historical_schema = record.get("historicalSchema", {})
        current_schema = record.get("currentSchema", {})

        data_type = (
            dataset_field.get("actualDataType")
            or current_schema.get("colSchema")
            or historical_schema.get("colSchema")
        )

        historical_type = historical_schema.get("colSchema")
        current_type = current_schema.get("colSchema")

        schema_changed = (
            historical_type is not None
            and current_type is not None
            and historical_type != current_type
        )

        mean_value = (
            CollibraDQClient.safe_float(dataset_field.get("meanAbsNum"))
            if dataset_field.get("meanAbsNum") is not None
            else CollibraDQClient.safe_float(dataset_field.get("meanAbs"))
        )

        return {
            "dataset": record.get("dataset"),
            "column": record.get("colName"),
            "run_id": dataset_field.get("runId"),

            "data_type": {
                "historical": historical_type,
                "current": current_type,
                "detected": data_type,
                "changed": schema_changed,
            },

            "current_profile": {
                "null_percent": dataset_field.get("nullRatio"),
                "empty_percent": dataset_field.get("emptyRatio"),
                "filled_percent": dataset_field.get("filledRatio"),

                "distinct_count": dataset_field.get("uniqueCnt"),
                "distinct_ratio": dataset_field.get("uniqueRatio"),

                "minimum": (
                    dataset_field.get("minAbsNum")
                    if dataset_field.get("minAbsNum") is not None
                    else dataset_field.get("minAbs")
                ),

                "maximum": (
                    dataset_field.get("maxAbsNum")
                    if dataset_field.get("maxAbsNum") is not None
                    else dataset_field.get("maxAbs")
                ),

                "mean": mean_value,

                "actual_data_type": dataset_field.get("actualDataType"),
                "pass_fail": dataset_field.get("passFail"),
            },

            "baseline_profile": {
                "average_null_percent": baseline.get("avg_nulls"),
                "average_empty_percent": baseline.get("avg_empties"),
                "average_filled_percent": baseline.get("avg_filled_ratio"),

                "average_distinct_count": baseline.get("avg_unique_cnt"),
                "median_distinct_count": baseline.get("median_unique_cnt"),

                "average_distinct_ratio": baseline.get("avg_uniques"),
                "median_distinct_ratio": baseline.get("median_unique_ratio"),

                "average_numeric_min": baseline.get("avg_min_abs_num"),
                "average_numeric_max": baseline.get("avg_max_abs_num"),
                "average_numeric_mean": baseline.get("avg_mean_abs_num"),
            },

            "delta": {
                "datatype": record.get("dtcntDelta"),
                "null": record.get("ncntDelta"),
                "empty": record.get("ecntDelta"),
                "distinct_count": record.get("uniqueDelta"),
                "distinct_percent": record.get("uniqueDeltaPercent"),

                "overall": record.get("changePercent"),
                "overall_with_uniques": record.get(
                    "changePercentWithUniques"
                ),
            },

            "generated_failure_condition": record.get("validValuesRule"),

            "schema_metadata": {
                "is_key": current_schema.get("isKey"),
                "is_pii": current_schema.get("isPii"),
                "is_mnpi": current_schema.get("isMnpi"),
                "is_masked": current_schema.get("isMasked"),
                "enabled": current_schema.get("enabled"),
            },

            "passed": dataset_field.get("passFail") == 1,
        }

    @staticmethod
    def _absolute_difference(
        current: Any,
        baseline: Any,
    ) -> Optional[float]:
        """Return the absolute difference between twoes."""
        current_number = CollibraDQClient.safe_float(current)
        baseline_number = CollibraDQClient.safe_float(baseline)

        if current_number is None or baseline_number is None:
            return None

        return abs(current_number - baseline_number)

    @staticmethod
    def _relative_change_percent(
        current: Any,
        baseline: Any,
    ) -> Optional[float]:
        """
        Calculate absolute relative percentage change.

        Example:
            baseline = 100
            current = 110
            result = 10.0

        If both values are zero, the result is zero.
        ne is zero but the current value is non-zero,
        the relative change is undefined and None is returned.
        """
        current_number = CollibraDQClient.safe_float(current)
        baseline_number = CollibraDQClient.safe_float(baseline)

        if current_number is None or baseline_number is None:
            return None

        if baseline_number == 0:
            return 0.0 if current_number == 0 else None

        return abs(
            (current_number - baseline_number)
            / baseline_number
            * 100
        )

    @staticmethod
    def _severity_from_change(
        absolute_change: Optional[float],
        thresholds: Mapping[str, float],
    ) -> str:
        """
        Convert an absolute percentage-point change into a severity.

        Default classification:
            < 1.0   -> NONE
            < 5.0   -> LOW
            < 10.0  -> MEDIUM
            >= 10.0 -> HIGH
        """
        if absolute_change is None:
            return "UNKNOWN"

        if absolute_change < thresholds["none_upper"]:
            return "NONE"

        if absolute_change < thresholds["low_upper"]:
            return "LOW"

        if absolute_change < thresholds["medium_upper"]:
            return "MEDIUM"

        return "HIGH"

    @staticmethod
    def _highest_severity(*severities: str) -> str:
        """Return the highest severity from the supplied values."""
        valid_severities = [
            severity
            for severity in severities
            if severity in SEVERITY_RANK
        ]

        if not valid_severities:
            return "UNKNOWN"

        return max(
            valid_severities,
            key=lambda severity: SEVERITY_RANK[severity],
        )

    @staticmethod
    def assess_profile_drift(
        normalised_record: Dict[str, Any],
        *,
        thresholds: Optional[Mapping[str, float]] = None,
    ) -> Dict[str, Any]:
        """
        Assess schema, completeness, and uniqueness drift for one
        record produced by normalize_profile_delta_record().

        Completeness severity is based on the larger of:
            - Null percentage-point change
            - Empty percentage-point change
            - Filled percentage-point change

        Uniqueness severity is based primarily on:
            - Unique-ratio percentage-point change

        The unique-count change is included as context, but it is not used
        as the primary severity measure because a small absolute count change
        can produce a very high relative percentage for low-cardinality fields.

        Schema changes are classified as CRITICAL.

        Args:
            normalised_record:
                Output from normalize_profile_delta_record().

            thresholds:
                Optional severity thresholds expressed in percentage points.

                Required keys:
                    none_upper
                    low_upper
                    medium_upper

        Returns:
            A copy of the normalised record with a `drift_assessment` object.
        """
        configured_thresholds = dict(
            thresholds or DEFAULT_DRIFT_THRESHOLDS
        )

        required_thresholds = {
            "none_upper",
            "low_upper",
            "medium_upper",
        }

        missing_thresholds = (
            required_thresholds - configured_thresholds.keys()
        )

        if missing_thresholds:
            raise ValueError(
                "Missing drift threshold settings: "
                f"{sorted(missing_thresholds)}"
            )

        if not (
            0 <= configured_thresholds["none_upper"]
            <= configured_thresholds["low_upper"]
            <= configured_thresholds["medium_upper"]
        ):
            raise ValueError(
                "Thresholds must satisfy: "
                "0 <= none_upper <= low_upper <= medium_upper"
            )

        current = normalised_record.get("current_profile") or {}
        baseline = normalised_record.get("baseline_profile") or {}
        data_type = normalised_record.get("data_type") or {}
        delta = normalised_record.get("delta") or {}

        # -------------------------------------------------------------
        # Schema drift
        # -------------------------------------------------------------

        historical_type = data_type.get("historical")
        current_type = data_type.get("current")
        detected_type = data_type.get("detected")

        schema_changed = bool(data_type.get("changed"))

        # Protect against a normaliser that did not set `changed`.
        if (
            historical_type is not None
            and current_type is not None
            and historical_type != current_type
        ):
            schema_changed = True

        detected_type_mismatch = (
            detected_type is not None
            and current_type is not None
            and detected_type != current_type
        )

        if schema_changed:
            schema_severity = "CRITICAL"
            schema_reason = (
                f"Column type changed from {historical_type!r} "
                f"to {current_type!r}."
            )
        elif detected_type_mismatch:
            schema_severity = "HIGH"
            schema_reason = (
                f"The detected type {detected_type!r} does not match "
                f"the current schema type {current_type!r}."
            )
        elif historical_type is None and current_type is None:
            schema_severity = "UNKNOWN"
            schema_reason = "Historical and current schema types are unavailable."
        else:
            schema_severity = "NONE"
            schema_reason = "No schema type change was detected."

        # -------------------------------------------------------------
        # Completeness drift
        # -------------------------------------------------------------

        current_null = CollibraDQClient.safe_float(current.get("null_percent"))
        baseline_null = CollibraDQClient.safe_float(
            baseline.get("average_null_percent")
        )
        null_change_pp = CollibraDQClient._absolute_difference(
            current_null,
            baseline_null,
        )

        current_empty = CollibraDQClient.safe_float(current.get("empty_percent"))
        baseline_empty = CollibraDQClient.safe_float(
            baseline.get("average_empty_percent")
        )
        empty_change_pp = CollibraDQClient._absolute_difference(
            current_empty,
            baseline_empty,
        )

        current_filled = CollibraDQClient.safe_float(current.get("filled_percent"))
        baseline_filled = CollibraDQClient.safe_float(
            baseline.get("average_filled_percent")
        )
        filled_change_pp = CollibraDQClient._absolute_difference(
            current_filled,
            baseline_filled,
        )

        available_completeness_changes = [
            value
            for value in (
                null_change_pp,
                empty_change_pp,
                filled_change_pp,
            )
            if value is not None
        ]

        completeness_max_change_pp = (
            max(available_completeness_changes)
            if available_completeness_changes
            else None
        )

        completeness_severity = CollibraDQClient._severity_from_change(
            completeness_max_change_pp,
            configured_thresholds,
        )

        if completeness_max_change_pp is None:
            completeness_reason = (
                "Current or baseline completeness metrics are unavailable."
            )
        elif completeness_severity == "NONE":
            completeness_reason = (
                "Completeness differences are below the configured "
                f"{configured_thresholds['none_upper']} percentage-point "
                "noise threshold."
            )
        else:
            completeness_components = {
                "null percentage": null_change_pp,
                "empty percentage": empty_change_pp,
                "filled percentage": filled_change_pp,
            }

            largest_component = max(
                (
                    (name, value)
                    for name, value in completeness_components.items()
                    if value is not None
                ),
                key=lambda item: item[1],
            )

            completeness_reason = (
                f"The largest completeness change is in "
                f"{largest_component[0]} at "
                f"{largest_component[1]:.5f} percentage points."
            )

        # -------------------------------------------------------------
        # Uniqueness drift
        # -------------------------------------------------------------

        current_distinct_count = CollibraDQClient.safe_float(
            current.get("distinct_count")
        )
        baseline_distinct_count = CollibraDQClient.safe_float(
            baseline.get("average_distinct_count")
        )

        distinct_count_change = CollibraDQClient._absolute_difference(
            current_distinct_count,
            baseline_distinct_count,
        )

        distinct_count_relative_change_pct = CollibraDQClient._relative_change_percent(
            current_distinct_count,
            baseline_distinct_count,
        )

        current_distinct_ratio = CollibraDQClient.safe_float(
            current.get("distinct_ratio")
        )
        baseline_distinct_ratio = CollibraDQClient.safe_float(
            baseline.get("average_distinct_ratio")
        )

        # Collibra's uniqueRatio values in your payload are fractions:
        # 0.255869 means approximately 25.5869%, not 0.255869%.
        distinct_ratio_change_fraction = CollibraDQClient._absolute_difference(
            current_distinct_ratio,
            baseline_distinct_ratio,
        )

        distinct_ratio_change_pp = (
            distinct_ratio_change_fraction * 100
            if distinct_ratio_change_fraction is not None
            else None
        )

        uniqueness_severity = CollibraDQClient._severity_from_change(
            distinct_ratio_change_pp,
            configured_thresholds,
        )

        if distinct_ratio_change_pp is None:
            uniqueness_reason = (
                "Current or baseline distinct-ratio metrics are unavailable."
            )
        elif uniqueness_severity == "NONE":
            uniqueness_reason = (
                "The distinct-ratio difference is below the configured "
                f"{configured_thresholds['none_upper']} percentage-point "
                "noise threshold."
            )
        else:
            uniqueness_reason = (
                f"The distinct ratio changed by "
                f"{distinct_ratio_change_pp:.5f} percentage points."
            )

        # -------------------------------------------------------------
        # Overall assessment
        # -------------------------------------------------------------

        overall_severity = CollibraDQClient._highest_severity(
            schema_severity,
            completeness_severity,
            uniqueness_severity,
        )

        triggered_dimensions = [
            dimension
            for dimension, severity in (
                ("schema", schema_severity),
                ("completeness", completeness_severity),
                ("uniqueness", uniqueness_severity),
            )
            if SEVERITY_RANK.get(severity, -1) > SEVERITY_RANK["NONE"]
        ]

        if overall_severity == "UNKNOWN":
            overall_reason = (
                "Insufficient information is available to assess drift."
            )
        elif overall_severity == "NONE":
            overall_reason = (
                "No material schema, completeness, or uniqueness drift "
                "was detected."
            )
        else:
            overall_reason = (
                f"The highest detected severity is {overall_severity}. "
                f"Triggered dimensions: {', '.join(triggered_dimensions)}."
            )

        drift_assessment = {
            "schema_drift": {
                "historical_type": historical_type,
                "current_type": current_type,
                "detected_type": detected_type,
                "changed": schema_changed,
                "detected_type_mismatch": detected_type_mismatch,
                "severity": schema_severity,
                "reason": schema_reason,
            },
            "completeness_drift": {
                "null": {
                    "baseline_percent": baseline_null,
                    "current_percent": current_null,
                    "absolute_change_pp": null_change_pp,
                },
                "empty": {
                    "baseline_percent": baseline_empty,
                    "current_percent": current_empty,
                    "absolute_change_pp": empty_change_pp,
                },
                "filled": {
                    "baseline_percent": baseline_filled,
                    "current_percent": current_filled,
                    "absolute_change_pp": filled_change_pp,
                },
                "maximum_absolute_change_pp": (
                    completeness_max_change_pp
                ),
                "collibra_null_delta": delta.get("null"),
                "collibra_empty_delta": delta.get("empty"),
                "severity": completeness_severity,
                "reason": completeness_reason,
            },
            "uniqueness_drift": {
                "distinct_count": {
                    "baseline": baseline_distinct_count,
                    "current": current_distinct_count,
                    "absolute_change": distinct_count_change,
                    "relative_change_percent": (
                        distinct_count_relative_change_pct
                    ),
                },
                "distinct_ratio": {
                    "baseline_fraction": baseline_distinct_ratio,
                    "current_fraction": current_distinct_ratio,
                    "absolute_change_pp": distinct_ratio_change_pp,
                },
                "collibra_distinct_count_delta": (
                    delta.get("distinct_count")
                ),
                "collibra_distinct_percent_delta": (
                    delta.get("distinct_percent")
                ),
                "severity": uniqueness_severity,
                "reason": uniqueness_reason,
            },
            "overall_severity": overall_severity,
            "overall_reason": overall_reason,
            "thresholds_percentage_points": configured_thresholds,
        }

        # Return a new object instead of modifying the caller's object.
        return {
            **normalised_record,
            "drift_assessment": drift_assessment,
        }

    def get_topn_bottomn_by_field(self, dataset: str, run_date: str, fieldnm: str) -> dict[str, Any]:
        """GET /v2/gettopandbottomnnsortedbyfield?dataset={dataset}&runId={run_date}&fieldnm={fieldnm}&sort=DESC 
        to retrieve the topN-bottomN for a specific field in a dataset run.

        Args:
            dataset (str): The dataset identifier.
            run_date (str): The run date of the dataset.
            fieldnm (str): The field name to retrieve topN-bottomN for.

        Returns:
            list[dict[str, Any]]: 
            The topN-bottomN as a list of dictionaries for the specific field, example:
        [
            {
                "id": 0,
                "dataset": "ds_ods_jp_itg_mdm_employee",
                "runId": "2026-09-09T16:00:00.000+0000",
                "fieldNm": "modified_by",
                "fieldFunc": "TOPN",
                "fieldValue": "system",
                "uniqueCnt": 13733,
                "updtTs": "2026-09-10T02:08:36.938+0000"
            }
        ]
        """
        return self._request(
            "GET", 
            f"/v2/gettopandbottomnnsortedbyfield", 
            params={"dataset": dataset, "runId": run_date, "fieldnm": fieldnm, "sort": "DESC"}
        )

    @staticmethod
    def normalize_topn_bottomn(records: List[Dict[str, Any]]) -> Dict[str, Any]:
        """
        Normalize Collibra TOPN/BOTTOMN API response into
        a compact structure
        
        Args:
            records (List[Dict[str, Any]]): The raw response from the Collibra TOPN/BOTTOMN API.

        Returns:
            Dict[str, Any]: A normalized, compact structure representing the topN-bottomN data, example:
        {
            "dataset": "ds_ods_jp_itg_mdm_employee",
            "columns": ["cost_center"],
            "run_id": "2026-09-09T16:00:00.000+0000",
            "profile_type": "TOPN",
            "generated_at": "2026-09-10T02:08:36.929+0000",
            "cost_center": 
            [
                {
                "value": null,
                "count": 11189
                },
                {
                "value": "JPE8508",
                "count": 330
                },
                {
                "value": "JPE8103",
                "count": 282
                }
            ]
        }
        """

        if not records:
            return {}

        first = records[0]
        columns = set(list({row.get("fieldNm") for row in records}))
        profile = {
            "dataset": first.get("dataset"),
            "columns": list(columns),
            "run_id": first.get("runId"),
            "profile_type": first.get("fieldFunc"),
            "generated_at": first.get("updtTs"),
        }
        # Extract all column names from the records
        for col in columns:
            col_records = [row for row in records if row.get("fieldNm") == col]
            values = []
            for row in col_records:
                value = row.get("fieldValue")
                if value in ("null", "NULL", ""):
                    value = None
                values.append(
                    {
                        "value": value,
                        "count": int(row["uniqueCnt"])
                        if row.get("uniqueCnt") is not None
                        else None,
                    }
                )
            profile[col] = values

        return profile

    def get_topn_bottomn_sorted(self, dataset: str, run_date: str, sort: str = "DESC") -> list[dict[str, Any]]:
        """GET /v2/gettopandbottomnnsorted?dataset={dataset}&runId={run_date}&sort={sort} 
        to retrieve the topN-bottomN for all fields in a dataset run, sorted by the specified order.

        Args:
            dataset (str): The dataset identifier.
            run_date (str): The run date of the dataset.
            sort (str, optional): The sort order, either "ASC" or "DESC". Defaults to "DESC".

        Returns:
            list[dict[str, Any]]: The topN-bottomN as a list of dictionaries for all fields.
        example:
        [
            {
                "id": 0,
                "dataset": "ds_ods_jp_itg_mdm_employee",
                "runId": "2026-09-09T16:00:00.000+0000",
                "fieldNm": "band_code",
                "fieldFunc": "TOPN",
                "fieldValue": "null",
                "uniqueCnt": 19643,
                "updtTs": "2026-09-10T02:08:36.596+0000"
            },
            {
                "id": 0,
                "dataset": "ds_ods_jp_itg_mdm_employee",
                "runId": "2026-09-09T16:00:00.000+0000",
                "fieldNm": "modified_by",
                "fieldFunc": "TOPN",
                "fieldValue": "system",
                "uniqueCnt": 13733,
                "updtTs": "2026-09-10T02:08:36.938+0000"
            }
        ]
        """
        return self._request(
            "GET", 
            f"/v2/gettopandbottomnnsorted", 
            params={"dataset": dataset, "runId": run_date, "sort": sort}
        )
    

    # ------------------------------------------------------------------
    # Write: create / update dataset definitions
    # ------------------------------------------------------------------
    def build_updated_dataset_def(self, dataset: str, patch: dict[str, Any]) -> dict[str, Any]:
        """Fetch the current DatasetDef and shallow-merge `patch` on top of it.

        Collibra DQ's /v3/datasetDefs PUT expects the full object, so callers
        should preview the merged result (this method) before calling
        `update_dataset_def` to actually write it.
        """
        current = self.get_dataset_def(dataset) or {"dataset": dataset}
        merged = copy.deepcopy(current)
        merged.update(patch)
        return merged

    def update_dataset_def(self, dataset: str, payload: dict[str, Any]) -> dict[str, Any]:
        """PUT the full DatasetDef payload. Callers must confirm before invoking this."""
        return self._request("PUT", f"/v3/datasetDefs", json=payload)

    def create_dataset_def(self, payload: dict[str, Any]) -> dict[str, Any]:
        """POST a new DatasetDef. Callers must confirm before invoking this."""
        return self._request("POST", "/v3/datasetDefs", json=payload)

    def get_alert_dataset(self, dataset: int) -> dict[str, Any]:
        """GET /v3/alerts/{dataset} to retrieve the alert for a specific dataset."""
        return self._request("GET", f"/v3/alerts/{dataset}")

    def create_alert(self, payload: dict[str, Any]) -> dict[str, Any]:
        """POST /v3/alerts to configure a dataset alert (e.g. low score email)."""
        return self._request("POST", "/v3/alerts", json=payload)

    def run_job(self, dataset: str, run_date: str) -> dict[str, Any]:
        """POST /v3/jobs/run to trigger a dataset run so a new definition takes effect."""
        return self._request("POST", "/v3/jobs/run", params={"dataset": dataset, "runDate": run_date})

    def set_boundary_suppress(
        self,
        dataset: str,
        run_id: str,
        item: str,
        metric_type: str,
        suppress: int,
        retrain: str = "true",
    ) -> dict[str, Any]:
        """POST /v2/set-boundary-suppress to suppress/unsuppress an adaptive rule metric
        (e.g. NULL/EMPTY/CARDINALITY/... for a column, or ROW_COUNT/TIME) for a dataset run.
        Does not trigger a job run -- takes effect immediately."""
        return self._request(
            "POST",
            "/v2/set-boundary-suppress",
            params={
                "dataset": dataset,
                "runId": run_id,
                "item": item,
                "metricType": metric_type,
                "suppress": suppress,
                "retrain": retrain,
            },
        )

    # ------------------------------------------------------------------
    # Write: custom rules
    # ------------------------------------------------------------------
    def create_rule(self, payload: dict[str, Any]) -> dict[str, Any]:
        """POST /v3/rules to create a new custom DQ rule, or update an existing one
        (matched by dataset+ruleNm) -- there is no separate PUT endpoint for rules."""
        return self._request("POST", "/v3/rules", json=payload)

    def delete_rule(self, dataset: str, rule_nm: str) -> dict[str, Any]:
        """DELETE /v3/rules to remove a custom DQ rule identified by dataset and ruleNm."""
        return self._request("DELETE", "/v3/rules", params={"dataset": dataset, "ruleName": rule_nm})

    # ------------------------------------------------------------------
    # Write: business units
    # ------------------------------------------------------------------
    def create_business_unit(self, name: str, sub_id: Optional[int] = None) -> dict[str, Any]:
        """POST /v2/business-unit to create a market (sub_id=None) or a project under one."""
        return self._request("POST", "/v2/business-unit", params={"name": name, "subId": sub_id})

    def assign_business_unit_to_dataset(self, business_unit_id: int, dataset: str) -> dict[str, Any]:
        """POST /v2/business-unit-to-dataset to attach a business unit to a dataset."""
        return self._request(
            "POST",
            "/v2/business-unit-to-dataset",
            params={"business_unit_id": business_unit_id, "dataset": dataset}
        )

    def update_business_unit_to_dataset(self, id: int, business_unit_id: int, dataset: str) -> dict[str, Any]:
        """PUT /v2/business-unit-to-dataset to update the attachment of a business unit to a dataset."""
        return self._request(
            "PUT", 
            "/v2/business-unit-to-dataset", 
            params={"id": id, "business_unit_id": business_unit_id, "dataset": dataset}
            )
