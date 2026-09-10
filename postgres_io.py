"""
Reusable PostgreSQL read/write helpers for pipeline scripts.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Sequence
import json
import logging

import pandas as pd
import psycopg2
import psycopg2.extras as extras


logger = logging.getLogger(__name__)


def _clean_value(v: Any) -> Any:
    """
    Normalize a cell value for psycopg2: JSON-encode list/dict/tuple values
    (e.g. nested CDQ type definitions), and convert NaN/NaT to None. pd.isna()
    raises on array-like values, so those are handled before calling it.
    """
    if isinstance(v, (list, dict, tuple)):
        return json.dumps(v)
    try:
        return None if pd.isna(v) else v
    except (TypeError, ValueError):
        return v


@dataclass(frozen=True)
class PostgresSettings:
    host: str
    port: int
    dbname: str
    user: str
    password: str
    table: str
    sslmode: str = "require"


def build_settings(
    host: str | None,
    port: int | str | None,
    dbname: str | None,
    user: str | None,
    password: str | None,
    table: str | None,
    sslmode: str = "require",
) -> PostgresSettings | None:
    """Return settings when all required DB credentials are present."""
    if not all([host, dbname, user, password, table]):
        return None
    return PostgresSettings(
        host=str(host),
        port=int(port or 5432),
        dbname=str(dbname),
        user=str(user),
        password=str(password),
        table=str(table),
        sslmode=sslmode,
    )


def load_db_credentials(dbutils, secret_scope: str, config_module) -> dict:
    """
    Load host/port/dbname/user/password for PostgreSQL from Databricks secrets
    (scope=secret_scope), falling back to config_module.DB_* attributes.
    """
    def _get(key: str, default):
        if dbutils is None:
            return default
        try:
            return dbutils.secrets.get(scope=secret_scope, key=key)
        except Exception:
            return default

    return {
        "host": _get("db_host", getattr(config_module, "DB_HOST", None)),
        "port": _get("db_port", getattr(config_module, "DB_PORT", None)),
        "dbname": _get("db_name", getattr(config_module, "DB_NAME", None)),
        "user": _get("db_user", getattr(config_module, "DB_USER", None)),
        "password": _get("db_password", getattr(config_module, "DB_PASSWORD", None)),
    }


def settings_for_table(
    db_credentials: dict,
    table: str | None,
    sslmode: str = "require",
) -> PostgresSettings | None:
    """Build PostgresSettings for a specific table using shared connection credentials."""
    return build_settings(
        host=db_credentials.get("host"),
        port=db_credentials.get("port"),
        dbname=db_credentials.get("dbname"),
        user=db_credentials.get("user"),
        password=db_credentials.get("password"),
        table=table,
        sslmode=sslmode,
    )


def connect(settings: PostgresSettings):
    """Create a psycopg2 connection using shared settings."""
    return psycopg2.connect(
        host=settings.host,
        port=settings.port,
        dbname=settings.dbname,
        user=settings.user,
        password=settings.password,
        sslmode=settings.sslmode,
    )


def read_sql(query: str, settings: PostgresSettings) -> pd.DataFrame:
    """Read query results into a DataFrame."""
    with connect(settings) as conn:
        return pd.read_sql(query, conn)


def read_table(settings: PostgresSettings, columns: Sequence[str] | None = None) -> pd.DataFrame:
    """Read a whole table (or selected columns) from PostgreSQL."""
    if columns:
        quoted_cols = ", ".join([f'"{c}"' for c in columns])
    else:
        quoted_cols = "*"
    query = f"SELECT {quoted_cols} FROM {settings.table}"
    return read_sql(query, settings)


def insert_on_conflict_do_nothing(
    df: pd.DataFrame,
    settings: PostgresSettings,
    insert_columns: Sequence[str],
    conflict_columns: Sequence[str],
    page_size: int = 500,
) -> int:
    """
    Bulk insert rows using execute_values and ON CONFLICT DO NOTHING.
    Returns inserted row count attempted.
    """
    if df.empty:
        return 0

    for col in insert_columns:
        if col not in df.columns:
            df[col] = None

    df_to_insert = df[list(insert_columns)]
    rows = [tuple(_clean_value(v) for v in r) for r in df_to_insert.to_numpy()]

    quoted_insert_cols = ",".join([f'"{c}"' for c in insert_columns])
    quoted_conflict_cols = ",".join([f'"{c}"' for c in conflict_columns])

    sql = f"""
        INSERT INTO {settings.table} ({quoted_insert_cols})
        VALUES %s
        ON CONFLICT ({quoted_conflict_cols}) DO NOTHING
    """

    with connect(settings) as conn:
        with conn.cursor() as cur:
            extras.execute_values(cur, sql, rows, page_size=page_size)

    return len(rows)


def filter_new_rows_by_keys(
    df: pd.DataFrame,
    existing_df: pd.DataFrame,
    key_columns: Iterable[str],
) -> pd.DataFrame:
    """Return rows from df whose key tuple does not exist in existing_df."""
    key_columns = list(key_columns)
    if df.empty:
        return df.copy()
    if existing_df.empty:
        return df.copy()

    df_idx = df.set_index(key_columns).index
    existing_idx = existing_df.set_index(key_columns).index
    return df[~df_idx.isin(existing_idx)].copy()


def upsert_dataframe(
    df: pd.DataFrame,
    settings: PostgresSettings,
    key_columns: Sequence[str],
    all_columns: Sequence[str],
    change_detect_columns: Sequence[str] | None = None,
    page_size: int = 500,
) -> int:
    """
    Insert new rows and update existing rows (ON CONFLICT DO UPDATE) keyed on
    key_columns. If change_detect_columns is given, the UPDATE only fires when
    at least one of those columns differs from the stored row (columns not in
    change_detect_columns, e.g. a "latest run id", are still always refreshed
    whenever an update fires).
    """
    if df.empty:
        return 0

    df = df.copy()
    for col in all_columns:
        if col not in df.columns:
            df[col] = None
    df_to_write = df[list(all_columns)]
    rows = [tuple(_clean_value(v) for v in r) for r in df_to_write.to_numpy()]

    quoted_all = ",".join(f'"{c}"' for c in all_columns)
    quoted_keys = ",".join(f'"{c}"' for c in key_columns)
    update_cols = [c for c in all_columns if c not in key_columns]

    if not update_cols:
        return insert_on_conflict_do_nothing(df, settings, all_columns, key_columns, page_size)

    set_clause = ",".join(f'"{c}" = EXCLUDED."{c}"' for c in update_cols)

    where_sql = ""
    if change_detect_columns:
        conditions = [
            f'{settings.table}."{c}" IS DISTINCT FROM EXCLUDED."{c}"'
            for c in change_detect_columns
        ]
        where_sql = f"WHERE {' OR '.join(conditions)}"

    sql = f"""
        INSERT INTO {settings.table} ({quoted_all})
        VALUES %s
        ON CONFLICT ({quoted_keys}) DO UPDATE SET {set_clause}
        {where_sql}
    """

    with connect(settings) as conn:
        with conn.cursor() as cur:
            extras.execute_values(cur, sql, rows, page_size=page_size)

    return len(rows)


def delete_rows_not_in_keys(
    df: pd.DataFrame,
    settings: PostgresSettings,
    group_columns: Sequence[str],
    key_columns: Sequence[str],
) -> int:
    """
    For each distinct group (e.g. dataset) present in df, delete DB rows in
    that group whose full key tuple is no longer present in df. Used to prune
    rows (e.g. columns) that were removed from a dataset's latest definition.
    """
    if df.empty:
        return 0

    non_group_keys = [c for c in key_columns if c not in group_columns]
    if not non_group_keys:
        return 0

    group_where = " AND ".join(f'"{c}" = %s' for c in group_columns)
    deleted = 0

    with connect(settings) as conn:
        with conn.cursor() as cur:
            for group_values, grp_df in df.groupby(list(group_columns)):
                if not isinstance(group_values, tuple):
                    group_values = (group_values,)

                if len(non_group_keys) == 1:
                    col = non_group_keys[0]
                    flat_keys = tuple(grp_df[col].dropna().tolist())
                    if not flat_keys:
                        continue
                    sql = f'DELETE FROM {settings.table} WHERE {group_where} AND "{col}" NOT IN %s'
                    cur.execute(sql, (*group_values, flat_keys))
                else:
                    key_tuples = tuple(tuple(r) for r in grp_df[non_group_keys].to_numpy())
                    if not key_tuples:
                        continue
                    key_cols_sql = ",".join(f'"{c}"' for c in non_group_keys)
                    sql = (
                        f"DELETE FROM {settings.table} WHERE {group_where} "
                        f"AND ({key_cols_sql}) NOT IN %s"
                    )
                    cur.execute(sql, (*group_values, key_tuples))
                deleted += cur.rowcount

    return deleted
