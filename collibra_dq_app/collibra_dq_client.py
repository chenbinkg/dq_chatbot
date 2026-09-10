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

import copy
import logging
import os
import sys
from typing import Any, Optional

import requests
import urllib3

# token_manager.py lives one level up (shared with dq_automation's copy).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from token_manager import CollibraTokenManager

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

logger = logging.getLogger(__name__)


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
    # Read: dataset definitions / findings / rules
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
