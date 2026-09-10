"""
Business unit inference for Collibra DQ datasets.

A business unit is a two-level hierarchy in Collibra:
  market  -> {"id": 1,  "name": "CN",             "subId": null}
  project -> {"id": 44, "name": "CN - ANGen MAF", "subId": 1}

So a dataset's business unit name is always "<market> - <project>", and the
project row's `subId` points at its parent market's `id`.

This module derives the market/project from the dataset's schema and name, and
splits an existing business unit list into markets and the distinct project
names available for the user to choose from.
"""

from __future__ import annotations

import re
from typing import Any, Optional

MARKETS = ["ANZ", "CN", "HK", "JP", "KR", "REGION", "TW", "VN"]

# Schema names that don't carry their market in the name itself.
SCHEMA_MARKET_OVERRIDES = {
    "pixonomy": "REGION",
    "na": "KR",
    "kr": "KR",
}


def split_business_units(business_units: list[dict[str, Any]]) -> dict[str, Any]:
    """Split a /v2/business-unit response into markets, projects and distinct project names."""
    markets = {bu["name"]: bu for bu in business_units if bu.get("subId") is None and bu.get("name")}
    projects = [bu for bu in business_units if bu.get("subId") is not None]

    project_names: set[str] = set()
    for bu in projects:
        name = bu.get("name") or ""
        if " - " in name:
            project_names.add(name.split(" - ", 1)[1].strip())

    return {
        "markets": markets,
        "projects": projects,
        "project_names": sorted(project_names),
    }


def infer_market(db_mn: str = "", dataset: str = "", table_mn: str = "") -> Optional[str]:
    """Infer the market from the schema first, then from tokens in the dataset/table name."""
    schema = (db_mn or "").strip().lower()
    if schema in SCHEMA_MARKET_OVERRIDES:
        return SCHEMA_MARKET_OVERRIDES[schema]

    # Schema/table are checked before the dataset name, whose "..._region_..." cluster
    # segment would otherwise always match the REGION market.
    for source in (schema, (table_mn or "").lower(), (dataset or "").lower()):
        tokens = set(re.split(r"[^a-z0-9]+", source))
        matches = [m for m in MARKETS if m.lower() in tokens]
        if matches:
            return matches[0]
    return None


def infer_project(dataset: str = "", db_mn: str = "", table_mn: str = "") -> str:
    """Infer the project name from the dataset naming patterns."""
    haystack = f"{dataset} {db_mn} {table_mn}".lower()
    if "angen_maf" in haystack or "angen maf" in haystack:
        return "ANGen MAF"
    if "angen" in haystack:
        return "ANGen"
    if "_dp_" in haystack:
        return "Data-Product"
    return "E.AI"


def suggest(
    business_units: list[dict[str, Any]],
    dataset: str,
    db_mn: str = "",
    table_mn: str = "",
) -> dict[str, Any]:
    """Suggest a "<market> - <project>" business unit and report what already exists."""
    split = split_business_units(business_units)
    market = infer_market(db_mn=db_mn, dataset=dataset, table_mn=table_mn)
    project = infer_project(dataset=dataset, db_mn=db_mn, table_mn=table_mn)

    market_bu = split["markets"].get(market) if market else None
    suggested_name = f"{market} - {project}" if market else None
    existing = next((bu for bu in split["projects"] if bu.get("name") == suggested_name), None)

    return {
        "dataset": dataset,
        "suggested_market": market,
        "suggested_project": project,
        "suggested_business_unit": suggested_name,
        "existing_business_unit": existing,
        "needs_creation": suggested_name is not None and existing is None,
        "market_id": market_bu.get("id") if market_bu else None,
        "available_markets": sorted(split["markets"]),
        "available_projects": split["project_names"],
        "projects_in_market": sorted(
            bu["name"].split(" - ", 1)[1]
            for bu in split["projects"]
            if market_bu and bu.get("subId") == market_bu.get("id") and " - " in (bu.get("name") or "")
        ),
    }
