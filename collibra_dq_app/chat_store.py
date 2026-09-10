"""
PostgreSQL persistence for the Collibra DQ chatbot:
- `public.dqm_chatbot_chat_history`: one row per chat session, upserted after
  every turn with the full conversation so far.
- `public.dqm_chatbot_change_history`: one row per individual field changed by
  `apply_dataset_change` (custom rule, profile setting, dataset definition
  field, email alert, or business unit), for audit/traceability.

Credentials come from the same DB_* environment variables used by
bu_mapping_reference.py. Call `ensure_tables()` once at app startup (or run
this module directly) to create the tables if they don't exist yet.

Session context (current app_user/session_id) is stored in a contextvar so
that collibra_tools.py's `apply_dataset_change` can log who/what session made
a change without every `@tool` function needing extra parameters -- app.py
sets it once per request via `set_session_context`.
"""

from __future__ import annotations

import contextvars
import json
import logging
import os
import sys
from typing import Any, Optional

import psycopg2.extras as extras

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from postgres_io import build_settings, connect

logger = logging.getLogger(__name__)

CHAT_HISTORY_TABLE = os.getenv("DQM_CHAT_HISTORY_TABLE", "public.dqm_chatbot_chat_history")
CHANGE_HISTORY_TABLE = os.getenv("DQM_CHANGE_HISTORY_TABLE", "public.dqm_chatbot_change_history")

CREATE_CHAT_HISTORY_SQL = f"""
CREATE TABLE IF NOT EXISTS {CHAT_HISTORY_TABLE} (
    id BIGSERIAL PRIMARY KEY,
    session_id TEXT NOT NULL UNIQUE,
    app_user TEXT NOT NULL,
    conversation_history JSONB NOT NULL,
    turn_count INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS dqm_chatbot_chat_history_user_idx ON {CHAT_HISTORY_TABLE} (app_user);
"""

CREATE_CHANGE_HISTORY_SQL = f"""
CREATE TABLE IF NOT EXISTS {CHANGE_HISTORY_TABLE} (
    id BIGSERIAL PRIMARY KEY,
    dataset TEXT NOT NULL,
    region TEXT NOT NULL,
    session_id TEXT,
    dataset_category TEXT NOT NULL CHECK (dataset_category IN ('New', 'Existing')),
    change_category TEXT NOT NULL,
    change_item TEXT NOT NULL,
    change_from TEXT,
    change_to TEXT,
    change_reason TEXT,
    change_by TEXT NOT NULL,
    changed_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS dqm_chatbot_change_history_dataset_idx ON {CHANGE_HISTORY_TABLE} (dataset, region);
CREATE INDEX IF NOT EXISTS dqm_chatbot_change_history_session_idx ON {CHANGE_HISTORY_TABLE} (session_id);
"""


def _settings_for(table: str):
    settings = build_settings(
        host=os.getenv("DB_HOST"),
        port=os.getenv("DB_PORT"),
        dbname=os.getenv("DB_NAME"),
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD"),
        table=table,
        sslmode=os.getenv("DB_SSLMODE", "require"),
    )
    if settings is None:
        raise ValueError("DB_HOST/DB_NAME/DB_USER/DB_PASSWORD must be set to use the chatbot history tables.")
    return settings


def ensure_tables() -> None:
    """Create both history tables (and their indexes) if they don't already exist."""
    settings = _settings_for(CHAT_HISTORY_TABLE)
    with connect(settings) as conn:
        with conn.cursor() as cur:
            cur.execute(CREATE_CHAT_HISTORY_SQL)
            cur.execute(CREATE_CHANGE_HISTORY_SQL)
        conn.commit()
    logger.info("Ensured %s and %s exist.", CHAT_HISTORY_TABLE, CHANGE_HISTORY_TABLE)


# ----------------------------------------------------------------------
# Session context: which logged-in user/session is driving the current
# agent turn, so tool code (collibra_tools.py) can attribute changes
# without threading extra parameters through every @tool function.
# ----------------------------------------------------------------------
_session_ctx: contextvars.ContextVar[dict[str, str]] = contextvars.ContextVar("dq_chat_session_ctx", default={})


def set_session_context(app_user: str, session_id: str) -> None:
    _session_ctx.set({"app_user": app_user or "unknown", "session_id": session_id or ""})


def get_session_context() -> dict[str, str]:
    return _session_ctx.get() or {"app_user": "unknown", "session_id": ""}


# ----------------------------------------------------------------------
# Chat history
# ----------------------------------------------------------------------
def log_chat_turn(app_user: str, session_id: str, conversation_history: list[dict[str, Any]]) -> None:
    """Upsert the running conversation for one session after each completed turn."""
    settings = _settings_for(CHAT_HISTORY_TABLE)
    sql = f"""
        INSERT INTO {CHAT_HISTORY_TABLE} (session_id, app_user, conversation_history, turn_count)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (session_id) DO UPDATE SET
            app_user = EXCLUDED.app_user,
            conversation_history = EXCLUDED.conversation_history,
            turn_count = EXCLUDED.turn_count,
            updated_at = now()
    """
    with connect(settings) as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (session_id, app_user, json.dumps(conversation_history, default=str), len(conversation_history)))
        conn.commit()


def get_latest_session(app_user: str) -> Optional[dict[str, Any]]:
    """Return the most recently updated chat session for this user (session_id,
    conversation_history, turn_count, updated_at), or None if they have no history yet.
    Used at login to offer resuming the previous conversation."""
    if not app_user:
        return None
    settings = _settings_for(CHAT_HISTORY_TABLE)
    sql = f"""
        SELECT session_id, conversation_history, turn_count, updated_at
        FROM {CHAT_HISTORY_TABLE}
        WHERE app_user = %s
        ORDER BY updated_at DESC
        LIMIT 1
    """
    with connect(settings) as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (app_user,))
            row = cur.fetchone()
    if not row:
        return None
    session_id, conversation_history, turn_count, updated_at = row
    # conversation_history comes back already parsed for JSONB columns, but fall back to
    # a manual json.loads in case the driver/column type ever returns a raw string.
    if isinstance(conversation_history, str):
        conversation_history = json.loads(conversation_history)
    return {
        "session_id": session_id,
        "conversation_history": conversation_history,
        "turn_count": turn_count,
        "updated_at": updated_at,
    }


# ----------------------------------------------------------------------
# Change history
# ----------------------------------------------------------------------
def log_changes(rows: list[dict[str, Any]]) -> None:
    """Bulk-insert change-history rows. Each row must have the columns of
    CHANGE_HISTORY_TABLE (session_id/change_reason may be None)."""
    if not rows:
        return
    columns = [
        "dataset",
        "region",
        "session_id",
        "dataset_category",
        "change_category",
        "change_item",
        "change_from",
        "change_to",
        "change_reason",
        "change_by",
    ]
    values = [tuple(row.get(col) for col in columns) for row in rows]
    quoted_cols = ",".join(f'"{c}"' for c in columns)
    sql = f"INSERT INTO {CHANGE_HISTORY_TABLE} ({quoted_cols}) VALUES %s"
    settings = _settings_for(CHANGE_HISTORY_TABLE)
    with connect(settings) as conn:
        with conn.cursor() as cur:
            extras.execute_values(cur, sql, values)
        conn.commit()


if __name__ == "__main__":
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass
    logging.basicConfig(level=logging.INFO)
    ensure_tables()
