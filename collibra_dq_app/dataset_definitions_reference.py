"""
Read-only lookups against `public.dqm_dataset_definitions` in PostgreSQL.

That table stores one row per (dataset, column) with the adaptive rule
configuration and current stats for that column (Null/Empty/Uniqueness/Min/
Max/Mean/Outliers/Shapes/Patterns checks, current null pct, etc.), plus
dataset-level fields (Run Id, Link Id, scheduler, dupes, business unit info)
repeated on every row for that dataset.

Reads use `postgres_io.read_sql` (../postgres_io.py); credentials come from
DB_* environment variables.
"""

from __future__ import annotations

import os
import sys
from typing import Any

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from postgres_io import build_settings, connect

DATASET_DEFINITIONS_TABLE = os.getenv("DQM_DATASET_DEFINITIONS_TABLE", "public.dqm_dataset_definitions")

# Dataset-level fields: identical on every row for a given dataset.
_DATASET_LEVEL_COLUMNS = [
    "dataset",
    "Run Id",
    "Link Id",
    "Date Filter",
    "Date Filter Key",
    "Scheduler",
    "Scheduled Freq",
    "Scheduled Time",
    "db_nm",
    "table_nm",
    "business_unit",
    "Market",
    "Project",
    "CDE",
]

# Per-column adaptive rule/check breakdown -- one row per source column.
_COLUMN_LEVEL_COLUMNS = [
    "col_name",
    "Data Type",
    "Row Count",
    "Execution Time",
    "Data Type Check",
    "Schema Change",
    "Dupes",
    "Custom Rules",
    "Null Values",
    "Empty Fields",
    "Uniqueness",
    "Min",
    "Max",
    "Mean",
    "Outliers",
    "Shapes",
    "Patterns",
]

_COLUMNS = _DATASET_LEVEL_COLUMNS + _COLUMN_LEVEL_COLUMNS + ["Current Null Pct"]


def _settings():
    settings = build_settings(
        host=os.getenv("DB_HOST"),
        port=os.getenv("DB_PORT"),
        dbname=os.getenv("DB_NAME"),
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD"),
        table=DATASET_DEFINITIONS_TABLE,
        sslmode=os.getenv("DB_SSLMODE", "require"),
    )
    if settings is None:
        raise ValueError("DB_HOST/DB_NAME/DB_USER/DB_PASSWORD must be set to read the dataset definitions table.")
    return settings


def get_dataset_definitions(dataset: str) -> dict[str, Any]:
    """Return the dataset-level adaptive rule config once, plus one row per source column
    with its adaptive check breakdown (Data Type Check, Schema Change, Dupes, Custom Rules,
    Null Values, Empty Fields, Uniqueness, Min, Max, Mean, Outliers, Shapes, Patterns)."""
    quoted_cols = ", ".join(f'"{c}"' for c in _COLUMNS)
    query = f"""
        SELECT {quoted_cols}
        FROM {DATASET_DEFINITIONS_TABLE}
        WHERE dataset = %(dataset)s
    """
    with connect(_settings()) as conn:
        df = pd.read_sql(query, conn, params={"dataset": dataset})
    if df.empty:
        return {"dataset_info": {}, "columns": []}

    first_row = df.iloc[0]
    dataset_info = {col: first_row[col] for col in _DATASET_LEVEL_COLUMNS}
    columns = df[_COLUMN_LEVEL_COLUMNS].to_dict(orient="records")
    return {"dataset_info": dataset_info, "columns": columns}


