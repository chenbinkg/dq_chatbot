"""
Reference lookups against `public.dqm_business_unit_mapping` in PostgreSQL.

That table stores the curated tagging already applied to existing datasets
("Data Domain", "subDomain", "connectionName", "db_nm", "table_nm", ...), so it
is the best ground truth for suggesting metaTags for a *new* dataset: find the
datasets whose names / connection / schema most resemble the new one and reuse
their Data Domain.

Reads use `postgres_io.read_table` (../postgres_io.py); credentials come from
DB_* environment variables.
"""

from __future__ import annotations

import difflib
import os
import re
import sys
from typing import Any, Optional

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from postgres_io import build_settings, read_table

BU_MAPPING_TABLE = os.getenv("DQM_BU_MAPPING_TABLE", "public.dqm_business_unit_mapping")

_COLUMNS = [
    "dataset",
    "business_unit",
    "Market",
    "Project",
    "CDE",
    "Data Domain",
    "subDomain",
    "connectionName",
    "db_nm",
    "table_nm",
]

_cache: dict[str, pd.DataFrame] = {}


def _settings():
    settings = build_settings(
        host=os.getenv("DB_HOST"),
        port=os.getenv("DB_PORT"),
        dbname=os.getenv("DB_NAME"),
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD"),
        table=BU_MAPPING_TABLE,
        sslmode=os.getenv("DB_SSLMODE", "require"),
    )
    if settings is None:
        raise ValueError("DB_HOST/DB_NAME/DB_USER/DB_PASSWORD must be set to read the business unit mapping table.")
    return settings


def load_mapping(refresh: bool = False) -> pd.DataFrame:
    """Load (and cache for the process lifetime) the curated dataset tagging table."""
    if refresh or BU_MAPPING_TABLE not in _cache:
        _cache[BU_MAPPING_TABLE] = read_table(_settings(), columns=_COLUMNS)
    return _cache[BU_MAPPING_TABLE]


def _tokens(value: Optional[str]) -> set[str]:
    return {t for t in re.split(r"[^a-z0-9]+", (value or "").lower()) if len(t) > 1}


def _score(row: pd.Series, name_tokens: set[str], dataset: str, conn_name: Optional[str], db_mn: Optional[str]) -> float:
    row_tokens = _tokens(row.get("dataset")) | _tokens(row.get("table_nm"))
    overlap = len(name_tokens & row_tokens) / len(name_tokens | row_tokens) if (name_tokens | row_tokens) else 0.0
    fuzzy = difflib.SequenceMatcher(None, dataset.lower(), str(row.get("dataset") or "").lower()).ratio()
    score = 0.6 * overlap + 0.4 * fuzzy
    if conn_name and str(row.get("connectionName") or "").lower() == conn_name.lower():
        score += 0.15
    if db_mn and str(row.get("db_nm") or "").lower() == db_mn.lower():
        score += 0.10
    return score


def find_similar_datasets(
    dataset: str,
    conn_name: Optional[str] = None,
    db_mn: Optional[str] = None,
    table_mn: Optional[str] = None,
    top_n: int = 10,
) -> dict[str, Any]:
    """Rank already-tagged datasets by similarity to a new dataset and summarise the
    Data Domain / subDomain values they use."""
    df = load_mapping()
    if df.empty:
        return {"dataset": dataset, "matches": [], "data_domain_votes": {}, "subdomain_votes": {}}

    name_tokens = _tokens(dataset) | _tokens(table_mn)
    scored = df.copy()
    scored["similarity"] = scored.apply(lambda r: _score(r, name_tokens, dataset, conn_name, db_mn), axis=1)
    top = scored.sort_values("similarity", ascending=False).head(max(1, min(int(top_n), 50)))

    domain_votes = top["Data Domain"].dropna().value_counts().to_dict()
    subdomain_votes: dict[str, int] = {}
    for value in top["subDomain"].dropna():
        for code in str(value).split(","):
            code = code.strip()
            if code:
                subdomain_votes[code] = subdomain_votes.get(code, 0) + 1

    return {
        "dataset": dataset,
        "matches": top[_COLUMNS + ["similarity"]].round({"similarity": 3}).to_dict(orient="records"),
        "data_domain_votes": domain_votes,
        "subdomain_votes": subdomain_votes,
    }


def data_domain_distribution() -> dict[str, int]:
    """Count of datasets per "Data Domain" value currently in use."""
    df = load_mapping()
    if df.empty:
        return {}
    return df["Data Domain"].dropna().value_counts().to_dict()
