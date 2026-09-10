"""
Static registry of known Collibra DQ source connections + a thin psycopg2
helper to pull column names / sample rows from Redshift for metaTag
suggestions.

There is no Collibra API to list connection names, so this table is
maintained by hand (see dq_chatbot/collibra_dq_app/.env.example for how to
override the two Redshift hosts). Connection strings that embed credentials
are intentionally NOT reproduced here -- only the pieces useful for choosing
a `connectionName` in a DatasetDef and, for Redshift, resolving which host to
query.
"""

from __future__ import annotations

import os
from typing import Any, Optional

# name, type, location (bucket / jdbc host+db, credentials stripped)
CONNECTIONS: list[dict[str, str]] = [
    {"connName": "CAE_Outputs_S3", "type": "S3", "location": "s3://itx-adj-ace-omnichannel/"},
    {"connName": "CDQ_Metastore", "type": "Postgres", "location": "collibradq-prod-postgres.cufto4yp2h96.ap-southeast-1.rds.amazonaws.com/idiscover"},
    {"connName": "Conn_Prod_DBx", "type": "Databricks", "location": "dbc-1d2e2f8a-2140.cloud.databricks.com (SQL warehouse; credentials in Collibra connection registry)"},
    {"connName": "Conn_Prod_ODS", "type": "Postgres", "location": "itx-adj-prod-postgresql.ap-southeast-1.rds.amazonaws.com/idiscover"},
    {"connName": "Conn_Prod_Redshift_JP_Local", "type": "Redshift", "location": "itx-adj-prod-dwh.cyl6xqaf72ng.ap-southeast-1.redshift.amazonaws.com:5439/idiscover"},
    {"connName": "Conn_Prod_Redshift_JP_Local_PD", "type": "Redshift", "location": "itx-adj-prod-dwh.cyl6xqaf72ng.ap-southeast-1.redshift.amazonaws.com:5439/idiscover"},
    {"connName": "Conn_Prod_Redshift_Region", "type": "Redshift", "location": "itx-adj-prod-dwh2.cyl6xqaf72ng.ap-southeast-1.redshift.amazonaws.com:5439/idiscover"},
    {"connName": "Conn_Prod_Redshift_Region_PD", "type": "Redshift", "location": "itx-adj-prod-dwh2.cyl6xqaf72ng.ap-southeast-1.redshift.amazonaws.com:5439/idiscover"},
    {"connName": "Conn_Prod_Redshift_Region_TW", "type": "Redshift", "location": "itx-adj-prod-dwh2.cyl6xqaf72ng.ap-southeast-1.redshift.amazonaws.com:5439/idiscover"},
    {"connName": "Conn_Prod_Redshift_VI", "type": "Redshift", "location": "itx-adj-osea-prod-dwh.cyl6xqaf72ng.ap-southeast-1.redshift.amazonaws.com:5439/oseaproddb"},
    {"connName": "Conn_Prod_Redshift_VN", "type": "Redshift", "location": "itx-adj-osea-prod-dwh.cyl6xqaf72ng.ap-southeast-1.redshift.amazonaws.com:5439/oseaproddb"},
    {"connName": "Conn_Prod_Reshift_JP_Local_latest", "type": "Redshift", "location": "itx-adj-prod-dwh2.cyl6xqaf72ng.ap-southeast-1.redshift.amazonaws.com:5439/idiscover"},
    {"connName": "conn_s3_angen_maf_files", "type": "S3", "location": "s3://itx-adj-pixonomy-prod/"},
    {"connName": "conn_s3_angen_unload", "type": "S3", "location": "s3://itx-adj-pixonomy-prod-dl/"},
    {"connName": "conn_s3_anz_enrichment_refined", "type": "S3", "location": "s3://itx-adj-anz-refined/"},
    {"connName": "Conn_S3_DQ_DE_Exfactory_SKU_Indication_Price", "type": "S3", "location": "s3://itx-adj-anz-raw/itx-adj-anz-qa/source/direct_file_formatted/Exfactory_SKU_Indication_Price"},
    {"connName": "Conn_S3_DQ_DE_Product_Attribute_Mapping", "type": "S3", "location": "s3://itx-adj-anz-raw/itx-adj-anz-qa/source/direct_file_formatted/Product_Attribute_Mapping"},
    {"connName": "conn_s3_sys_marketdef_indsplt_automation", "type": "S3", "location": "s3://itx-adj-mdm-ods-prod/"},
    {"connName": "s3_conn_angen_maf_files", "type": "S3", "location": "s3://itx-adj-pixonomy-prod/"},
    {"connName": "s3_conn_anz_enrichment", "type": "S3", "location": "s3://itx-adj-anz-raw"},
    {"connName": "s3_conn_kr_raw", "type": "S3", "location": "s3://itx-adj-kr-raw"},
]

REDSHIFT_CONN1_HOST = os.getenv("REDSHIFT_CONN1_HOST", "itx-adj-prod-dwh.cyl6xqaf72ng.ap-southeast-1.redshift.amazonaws.com")
REDSHIFT_CONN2_HOST = os.getenv("REDSHIFT_CONN2_HOST", "itx-adj-prod-dwh2.cyl6xqaf72ng.ap-southeast-1.redshift.amazonaws.com")


def list_connections(name_contains: str = "") -> list[dict[str, str]]:
    """Return the known connection registry, optionally filtered by substring on connName."""
    if not name_contains:
        return CONNECTIONS
    needle = name_contains.lower()
    return [c for c in CONNECTIONS if needle in c["connName"].lower()]


def resolve_conn_host(conn_name: str) -> Optional[str]:
    """Pick the Redshift host for a connection name using the "local"/"region" naming rule."""
    lowered = (conn_name or "").lower()
    if "local" in lowered:
        return REDSHIFT_CONN1_HOST
    if "region" in lowered:
        return REDSHIFT_CONN2_HOST
    return None


def sample_table(conn_name: str, db_mn: str, table_mn: str, limit: int = 10) -> dict[str, Any]:
    """Connect to the Redshift host resolved from `conn_name` and return column names + up
    to `limit` sample rows from `db_mn.table_mn`. Read-only (SELECT ... LIMIT).

    Requires REDSHIFT_USER / REDSHIFT_PASSWORD (and optionally REDSHIFT_DBNAME, default
    "idiscover") to be set in the environment.
    """
    import psycopg2  # imported lazily so the rest of the app works without it installed

    conn_host = resolve_conn_host(conn_name)
    if not conn_host:
        raise ValueError(f"Could not resolve a Redshift host for connection name '{conn_name}' (expected 'local' or 'region' in the name).")

    user = os.getenv("REDSHIFT_USER")
    password = os.getenv("REDSHIFT_PASSWORD")
    dbname = os.getenv("REDSHIFT_DBNAME", "idiscover")
    if not user or not password:
        raise ValueError("REDSHIFT_USER and REDSHIFT_PASSWORD must be set in the environment to sample Redshift tables.")

    safe_limit = max(1, min(int(limit), 50))

    conn = psycopg2.connect(host=conn_host, port=5439, dbname=dbname, user=user, password=password, connect_timeout=15)
    try:
        cur = conn.cursor()
        try:
            # db_mn/table_mn are provided by the trusted super-user via the chat UI, not
            # end-user input, but still quoted defensively against stray identifiers.
            cur.execute(f'SELECT * FROM "{db_mn}"."{table_mn}" LIMIT {safe_limit}')
            columns = [desc[0] for desc in cur.description]
            rows = cur.fetchall()
        finally:
            cur.close()
    finally:
        conn.close()

    return {
        "conn_name": conn_name,
        "conn_host": conn_host,
        "db_mn": db_mn,
        "table_mn": table_mn,
        "columns": columns,
        "sample_rows": [dict(zip(columns, row)) for row in rows],
    }


# cluster label -> host, per the "local"/"region" naming rule used for dataset names
REDSHIFT_CLUSTERS = {"local": REDSHIFT_CONN1_HOST, "region": REDSHIFT_CONN2_HOST}


def _table_exists(host: str, db_mn: str, table_mn: str) -> bool:
    import psycopg2

    user = os.getenv("REDSHIFT_USER")
    password = os.getenv("REDSHIFT_PASSWORD")
    dbname = os.getenv("REDSHIFT_DBNAME", "idiscover")
    if not user or not password:
        raise ValueError("REDSHIFT_USER and REDSHIFT_PASSWORD must be set in the environment to probe Redshift clusters.")

    conn = psycopg2.connect(host=host, port=5439, dbname=dbname, user=user, password=password, connect_timeout=15)
    try:
        with conn.cursor() as cur:
            # svv_tables covers both native and external/spectrum schemas.
            cur.execute(
                "SELECT 1 FROM svv_tables WHERE table_schema = %s AND table_name = %s LIMIT 1",
                (db_mn, table_mn),
            )
            return cur.fetchone() is not None
    finally:
        conn.close()


def detect_redshift_cluster(db_mn: str, table_mn: str) -> dict[str, Any]:
    """Probe both Redshift clusters for `db_mn.table_mn` and report which one(s) have it."""
    results: dict[str, Any] = {}
    for cluster, host in REDSHIFT_CLUSTERS.items():
        try:
            results[cluster] = {"host": host, "found": _table_exists(host, db_mn, table_mn)}
        except Exception as exc:
            results[cluster] = {"host": host, "found": False, "error": str(exc)}

    found = [c for c, r in results.items() if r.get("found")]
    return {
        "db_mn": db_mn,
        "table_mn": table_mn,
        "clusters": results,
        "matched_clusters": found,
        "cluster": found[0] if len(found) == 1 else None,
    }


def _resolve_clusters(cluster: Optional[str]) -> dict[str, str]:
    if not cluster:
        return REDSHIFT_CLUSTERS
    cluster_key = cluster.strip().lower()
    if cluster_key not in REDSHIFT_CLUSTERS:
        raise ValueError(f"Unknown cluster '{cluster}'. Use 'local' or 'region'.")
    return {cluster_key: REDSHIFT_CLUSTERS[cluster_key]}


def _redshift_credentials() -> tuple[str, str, str]:
    user = os.getenv("REDSHIFT_USER")
    password = os.getenv("REDSHIFT_PASSWORD")
    dbname = os.getenv("REDSHIFT_DBNAME", "idiscover")
    if not user or not password:
        raise ValueError("REDSHIFT_USER and REDSHIFT_PASSWORD must be set in the environment.")
    return user, password, dbname


def search_tables(keyword: str, cluster: Optional[str] = None, db_mn: Optional[str] = None, limit: int = 50) -> dict[str, Any]:
    """Search svv_tables (covers native + external/Spectrum schemas) for table names
    containing `keyword`, across one or both Redshift clusters. Read-only. Use this to find
    the physical schema.table backing a cross-reference before it has a DQ dataset yet."""
    import psycopg2

    needle = f"%{(keyword or '').strip().lower()}%"
    if needle == "%%":
        raise ValueError("keyword is required.")
    clusters = _resolve_clusters(cluster)
    user, password, dbname = _redshift_credentials()
    safe_limit = max(1, min(int(limit), 200))

    matches: list[dict[str, Any]] = []
    for cluster_name, host in clusters.items():
        query = "SELECT table_schema, table_name FROM svv_tables WHERE LOWER(table_name) LIKE %s"
        params: list[Any] = [needle]
        if db_mn:
            query += " AND table_schema = %s"
            params.append(db_mn)
        query += " ORDER BY table_schema, table_name LIMIT %s"
        params.append(safe_limit)

        conn = psycopg2.connect(host=host, port=5439, dbname=dbname, user=user, password=password, connect_timeout=15)
        try:
            with conn.cursor() as cur:
                cur.execute(query, params)
                matches.extend(
                    {"cluster": cluster_name, "table_schema": schema, "table_name": table}
                    for schema, table in cur.fetchall()
                )
        finally:
            conn.close()

    return {"keyword": keyword, "matches": matches}


def search_columns(
    keyword: str,
    cluster: Optional[str] = None,
    db_mn: Optional[str] = None,
    table_mn: Optional[str] = None,
    limit: int = 50,
) -> dict[str, Any]:
    """Search svv_columns for column names containing `keyword`, optionally scoped to a
    schema and/or table, across one or both Redshift clusters. Read-only. Use this to
    resolve the actual join/id column name (e.g. "source_id" vs "account_id") when
    constructing a custom rule that cross-references another table."""
    import psycopg2

    needle = f"%{(keyword or '').strip().lower()}%"
    if needle == "%%":
        raise ValueError("keyword is required.")
    clusters = _resolve_clusters(cluster)
    user, password, dbname = _redshift_credentials()
    safe_limit = max(1, min(int(limit), 200))

    matches: list[dict[str, Any]] = []
    for cluster_name, host in clusters.items():
        query = "SELECT table_schema, table_name, column_name FROM svv_columns WHERE LOWER(column_name) LIKE %s"
        params: list[Any] = [needle]
        if db_mn:
            query += " AND table_schema = %s"
            params.append(db_mn)
        if table_mn:
            query += " AND table_name = %s"
            params.append(table_mn)
        query += " ORDER BY table_schema, table_name, column_name LIMIT %s"
        params.append(safe_limit)

        conn = psycopg2.connect(host=host, port=5439, dbname=dbname, user=user, password=password, connect_timeout=15)
        try:
            with conn.cursor() as cur:
                cur.execute(query, params)
                matches.extend(
                    {"cluster": cluster_name, "table_schema": schema, "table_name": table, "column_name": column}
                    for schema, table, column in cur.fetchall()
                )
        finally:
            conn.close()

    return {"keyword": keyword, "matches": matches}


def run_readonly_query(cluster: str, query: str, limit: int = 20) -> dict[str, Any]:
    """Run a single read-only SELECT query against a Redshift cluster, capped at `limit`
    rows -- e.g. to test/troubleshoot a candidate custom-rule query (with `@dataset_name`
    tokens replaced by the real schema.table, since those tokens are Collibra-only
    substitutions and not valid raw SQL) before saving it. Rejects anything that isn't a
    single SELECT statement."""
    import psycopg2

    stripped = (query or "").strip().rstrip(";")
    if not stripped:
        raise ValueError("query is required.")
    if not stripped.lower().startswith("select") or ";" in stripped:
        raise ValueError("Only a single read-only SELECT statement is allowed.")

    clusters = _resolve_clusters(cluster)
    if len(clusters) != 1:
        raise ValueError("cluster is required (\"local\" or \"region\") to run a query.")
    cluster_name, host = next(iter(clusters.items()))
    user, password, dbname = _redshift_credentials()
    safe_limit = max(1, min(int(limit), 100))
    wrapped = f"SELECT * FROM ({stripped}) AS _rule_test LIMIT {safe_limit}"

    conn = psycopg2.connect(host=host, port=5439, dbname=dbname, user=user, password=password, connect_timeout=30)
    try:
        with conn.cursor() as cur:
            cur.execute(wrapped)
            columns = [d[0] for d in cur.description]
            rows = cur.fetchall()
    finally:
        conn.close()

    return {
        "cluster": cluster_name,
        "row_count": len(rows),
        "columns": columns,
        "sample_rows": [dict(zip(columns, row)) for row in rows],
        "query": wrapped,
    }


def parse_s3_path(s3_path: str) -> dict[str, Any]:
    """Parse an s3:// file path into its matching connection, schema (parent folder) and
    table (file name without extension)."""
    path = (s3_path or "").strip()
    if not path.lower().startswith("s3://"):
        raise ValueError(f"Expected an s3:// path, got '{s3_path}'.")

    parts = [p for p in path[len("s3://"):].split("/") if p]
    if len(parts) < 2:
        raise ValueError(f"S3 path '{s3_path}' does not include a file name.")

    file_name = parts[-1]
    schema = parts[-2]
    table = file_name.rsplit(".", 1)[0] if "." in file_name else file_name

    normalized = path.rstrip("/")
    candidates = [
        c for c in CONNECTIONS
        if c["type"] == "S3" and normalized.lower().startswith(c["location"].rstrip("/").lower())
    ]
    # Prefer the most specific (longest) matching location.
    candidates.sort(key=lambda c: len(c["location"]), reverse=True)

    return {
        "s3_path": s3_path,
        "connection_name": candidates[0]["connName"] if candidates else None,
        "matching_connections": [c["connName"] for c in candidates],
        "schema": schema,
        "table": table,
    }


def _parse_s3_uri(s3_path: str) -> tuple[str, str]:
    path = (s3_path or "").strip()
    if not path.lower().startswith("s3://"):
        raise ValueError(f"Expected an s3:// path, got '{s3_path}'.")
    bucket, _, key = path[len("s3://"):].partition("/")
    if not bucket:
        raise ValueError(f"Could not parse a bucket from '{s3_path}'.")
    return bucket, key


def list_s3_objects(prefix_path: str, max_keys: int = 50) -> dict[str, Any]:
    """List object keys under an s3:// bucket/prefix (folder), read-only (ListObjectsV2).
    Use this to find the exact file name when you know the bucket/folder but not the
    precise file, before calling sample_s3_file."""
    import boto3

    bucket, prefix = _parse_s3_uri(prefix_path)
    safe_max = max(1, min(int(max_keys), 200))
    client = boto3.client("s3")
    response = client.list_objects_v2(Bucket=bucket, Prefix=prefix, MaxKeys=safe_max)
    objects = [
        {
            "key": obj["Key"],
            "size_bytes": obj.get("Size"),
            "last_modified": obj["LastModified"].isoformat() if obj.get("LastModified") else None,
        }
        for obj in response.get("Contents", [])
    ]
    return {
        "bucket": bucket,
        "prefix": prefix,
        "objects": objects,
        "is_truncated": response.get("IsTruncated", False),
    }


def sample_s3_file(s3_path: str, limit: int = 10, max_bytes: int = 262144) -> dict[str, Any]:
    """Read the header + up to `limit` sample rows from a CSV file in S3, to help ground
    metaTag/business-domain suggestions and linkId choices in real data when building an
    s3-backed dataset. Read-only -- fetches at most `max_bytes` via a ranged GET so large
    files aren't fully downloaded, then parses whatever complete lines were retrieved."""
    import boto3
    import csv

    bucket, key = _parse_s3_uri(s3_path)
    if not key or key.endswith("/"):
        raise ValueError(f"'{s3_path}' does not look like a file path (missing object key).")

    safe_limit = max(1, min(int(limit), 50))
    safe_max_bytes = max(4096, min(int(max_bytes), 5 * 1024 * 1024))

    client = boto3.client("s3")
    head = client.head_object(Bucket=bucket, Key=key)
    file_size = head.get("ContentLength") or 0
    end_byte = max(min(safe_max_bytes, file_size) - 1, 0) if file_size else safe_max_bytes - 1

    obj = client.get_object(Bucket=bucket, Key=key, Range=f"bytes=0-{end_byte}")
    text = obj["Body"].read().decode("utf-8", errors="replace")
    truncated_read = bool(file_size) and end_byte + 1 < file_size

    lines = text.splitlines()
    if truncated_read and lines:
        # the ranged read may have cut the last line mid-row -- drop it
        lines = lines[:-1]

    rows = list(csv.reader(lines))
    columns = rows[0] if rows else []
    sample_rows = [dict(zip(columns, row)) for row in rows[1:1 + safe_limit]]

    return {
        "s3_path": s3_path,
        "bucket": bucket,
        "key": key,
        "file_size_bytes": file_size,
        "columns": columns,
        "sample_rows": sample_rows,
        "truncated_read": truncated_read,
    }


def check_link_id_uniqueness(cluster: str, db_mn: str, table_mn: str, link_id: list[str]) -> dict[str, Any]:
    """Check whether `link_id` columns uniquely identify rows in a Redshift table by
    listing up to 10 duplicated key combinations. Read-only."""
    import psycopg2

    host = REDSHIFT_CLUSTERS.get((cluster or "").strip().lower())
    if not host:
        raise ValueError(f"Unknown cluster '{cluster}'. Use 'local' or 'region'.")
    columns = [c for c in (link_id or []) if c]
    if not columns:
        raise ValueError("At least one linkId column is required.")

    user = os.getenv("REDSHIFT_USER")
    password = os.getenv("REDSHIFT_PASSWORD")
    dbname = os.getenv("REDSHIFT_DBNAME", "idiscover")
    if not user or not password:
        raise ValueError("REDSHIFT_USER and REDSHIFT_PASSWORD must be set in the environment.")

    col_sql = ", ".join(f'"{c}"' for c in columns)
    query = (
        f'SELECT {col_sql}, count(*) AS count FROM "{db_mn}"."{table_mn}" '
        f"GROUP BY {col_sql} HAVING count(*) > 1 LIMIT 10"
    )

    conn = psycopg2.connect(host=host, port=5439, dbname=dbname, user=user, password=password, connect_timeout=15)
    try:
        with conn.cursor() as cur:
            cur.execute(query)
            headers = [d[0] for d in cur.description]
            rows = cur.fetchall()
    finally:
        conn.close()

    duplicates = [dict(zip(headers, row)) for row in rows]
    return {
        "cluster": cluster,
        "db_mn": db_mn,
        "table_mn": table_mn,
        "link_id": columns,
        "is_unique": not duplicates,
        "duplicate_examples": duplicates,
        "query": query,
    }
