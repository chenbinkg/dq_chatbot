"""
PostgreSQL persistence for the Collibra DQ chatbot:
- `public.dqm_chatbot_chat_history`: one row per chat session, upserted after
  every turn with the full conversation so far.
- `public.dqm_chatbot_change_history`: one row per individual field changed by
  `apply_dataset_change` (custom rule, profile setting, dataset definition
  field, email alert, or business unit), for audit/traceability.
- `public.dqm_chatbot_agent_turn`: one normalized row per agent invocation.
- `public.dqm_chatbot_tool_execution`: ordered, sanitized tool calls per turn.
- `public.dqm_chatbot_response_feedback`: thumbs feedback and optional comments.

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
AGENT_TURN_TABLE = os.getenv("DQM_AGENT_TURN_TABLE", "public.dqm_chatbot_agent_turn")
TOOL_EXECUTION_TABLE = os.getenv("DQM_TOOL_EXECUTION_TABLE", "public.dqm_chatbot_tool_execution")
RESPONSE_FEEDBACK_TABLE = os.getenv("DQM_RESPONSE_FEEDBACK_TABLE", "public.dqm_chatbot_response_feedback")
UNRESOLVED_FEEDBACK_TABLE = os.getenv("DQM_UNRESOLVED_FEEDBACK_TABLE", "public.dqm_chatbot_unresolved_feedback")

CREATE_CHAT_HISTORY_SQL = f"""
CREATE TABLE IF NOT EXISTS {CHAT_HISTORY_TABLE} (
    id BIGSERIAL PRIMARY KEY,
    region TEXT NOT NULL,
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

CREATE_AGENT_TURN_SQL = f"""
CREATE TABLE IF NOT EXISTS {AGENT_TURN_TABLE} (
    turn_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    app_user TEXT NOT NULL,
    region TEXT NOT NULL,
    user_message_id TEXT NOT NULL UNIQUE,
    assistant_message_id TEXT NOT NULL UNIQUE,
    user_message TEXT NOT NULL,
    assistant_response TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('completed', 'interrupted', 'error')),
    stop_reason TEXT,
    duration_ms INTEGER NOT NULL CHECK (duration_ms >= 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS dqm_chatbot_agent_turn_session_idx ON {AGENT_TURN_TABLE} (session_id, created_at);
CREATE INDEX IF NOT EXISTS dqm_chatbot_agent_turn_user_idx ON {AGENT_TURN_TABLE} (app_user, created_at);
"""

CREATE_TOOL_EXECUTION_SQL = f"""
CREATE TABLE IF NOT EXISTS {TOOL_EXECUTION_TABLE} (
    execution_id TEXT PRIMARY KEY,
    turn_id TEXT NOT NULL REFERENCES {AGENT_TURN_TABLE} (turn_id) ON DELETE CASCADE,
    sequence_number INTEGER NOT NULL,
    tool_name TEXT NOT NULL,
    tool_input JSONB NOT NULL DEFAULT '{{}}'::jsonb,
    status TEXT NOT NULL CHECK (status IN ('completed', 'error')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (turn_id, sequence_number)
);
CREATE INDEX IF NOT EXISTS dqm_chatbot_tool_execution_turn_idx ON {TOOL_EXECUTION_TABLE} (turn_id, sequence_number);
CREATE INDEX IF NOT EXISTS dqm_chatbot_tool_execution_name_idx ON {TOOL_EXECUTION_TABLE} (tool_name, created_at);
"""

CREATE_RESPONSE_FEEDBACK_SQL = f"""
CREATE TABLE IF NOT EXISTS {RESPONSE_FEEDBACK_TABLE} (
    feedback_id TEXT PRIMARY KEY,
    assistant_message_id TEXT NOT NULL REFERENCES {AGENT_TURN_TABLE} (assistant_message_id) ON DELETE CASCADE,
    turn_id TEXT NOT NULL REFERENCES {AGENT_TURN_TABLE} (turn_id) ON DELETE CASCADE,
    session_id TEXT NOT NULL,
    app_user TEXT NOT NULL,
    vote SMALLINT NOT NULL CHECK (vote IN (-1, 1)),
    comment TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (app_user, assistant_message_id)
);
CREATE INDEX IF NOT EXISTS dqm_chatbot_response_feedback_turn_idx ON {RESPONSE_FEEDBACK_TABLE} (turn_id);
CREATE INDEX IF NOT EXISTS dqm_chatbot_response_feedback_user_idx ON {RESPONSE_FEEDBACK_TABLE} (app_user, created_at);
"""

CREATE_UNRESOLVED_FEEDBACK_SQL = f"""
CREATE TABLE IF NOT EXISTS {UNRESOLVED_FEEDBACK_TABLE} (
    feedback_id TEXT PRIMARY KEY,
    app_user TEXT NOT NULL,
    vote SMALLINT NOT NULL CHECK (vote IN (-1, 1)),
    comment TEXT,
    response_content TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS dqm_chatbot_unresolved_feedback_user_idx ON {UNRESOLVED_FEEDBACK_TABLE} (app_user, created_at);
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
    """Create chatbot persistence tables and their indexes if they do not exist."""
    settings = _settings_for(CHAT_HISTORY_TABLE)
    with connect(settings) as conn:
        with conn.cursor() as cur:
            cur.execute(CREATE_CHAT_HISTORY_SQL)
            cur.execute(CREATE_CHANGE_HISTORY_SQL)
            cur.execute(CREATE_AGENT_TURN_SQL)
            cur.execute(CREATE_TOOL_EXECUTION_SQL)
            cur.execute(CREATE_RESPONSE_FEEDBACK_SQL)
            cur.execute(CREATE_UNRESOLVED_FEEDBACK_SQL)
        conn.commit()
    logger.info("Ensured chatbot history, trace, and feedback tables exist.")


# ----------------------------------------------------------------------
# Session context: which logged-in user/session is driving the current
# agent turn, so tool code (collibra_tools.py) can attribute changes
# without threading extra parameters through every @tool function.
# ----------------------------------------------------------------------
_session_ctx: contextvars.ContextVar[dict[str, str]] = contextvars.ContextVar("dq_chat_session_ctx", default={})


def set_session_context(app_user: str, session_id: str, region: str = "") -> None:
    _session_ctx.set(
        {
            "app_user": app_user or "unknown",
            "session_id": session_id or "",
            "region": (region or "apac").strip().lower(),
        }
    )


def get_session_context() -> dict[str, str]:
    return _session_ctx.get() or {"app_user": "unknown", "session_id": "", "region": "apac"}


# ----------------------------------------------------------------------
# Chat history
# ----------------------------------------------------------------------
def log_chat_turn(app_user: str, session_id: str, region: str, conversation_history: list[dict[str, Any]]) -> None:
    """Upsert the running conversation for one session after each completed turn."""
    region = (region or "apac").strip().lower()
    settings = _settings_for(CHAT_HISTORY_TABLE)
    sql = f"""
        INSERT INTO {CHAT_HISTORY_TABLE} (region, session_id, app_user, conversation_history, turn_count)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (session_id) DO UPDATE SET
            region = EXCLUDED.region,
            app_user = EXCLUDED.app_user,
            conversation_history = EXCLUDED.conversation_history,
            turn_count = EXCLUDED.turn_count,
            updated_at = now()
    """
    with connect(settings) as conn:
        with conn.cursor() as cur:
            cur.execute(
                sql,
                (region, session_id, app_user, json.dumps(conversation_history, default=str), len(conversation_history)),
            )
        conn.commit()


def get_latest_session(app_user: str, region: Optional[str] = None) -> Optional[dict[str, Any]]:
    """Return the most recently updated chat session for this user (session_id,
    conversation_history, turn_count, updated_at), or None if they have no history yet.
    Used at login to offer resuming the previous conversation."""
    if not app_user:
        return None
    settings = _settings_for(CHAT_HISTORY_TABLE)
    params: list[Any] = [app_user]
    region_filter = ""
    if region:
        region_filter = "AND region = %s"
        params.append(region.strip().lower())
    sql = f"""
        SELECT region, session_id, conversation_history, turn_count, updated_at
        FROM {CHAT_HISTORY_TABLE}
        WHERE app_user = %s
        {region_filter}
        ORDER BY updated_at DESC
        LIMIT 1
    """
    with connect(settings) as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            row = cur.fetchone()
    if not row:
        return None
    row_region, session_id, conversation_history, turn_count, updated_at = row
    # conversation_history comes back already parsed for JSONB columns, but fall back to
    # a manual json.loads in case the driver/column type ever returns a raw string.
    if isinstance(conversation_history, str):
        conversation_history = json.loads(conversation_history)
    return {
        "region": row_region,
        "session_id": session_id,
        "conversation_history": conversation_history,
        "turn_count": turn_count,
        "updated_at": updated_at,
    }


# ----------------------------------------------------------------------
# Agent traces and response feedback
# ----------------------------------------------------------------------
def log_agent_turn(
    *,
    turn_id: str,
    session_id: str,
    app_user: str,
    region: str,
    user_message_id: str,
    assistant_message_id: str,
    user_message: str,
    assistant_response: str,
    status: str,
    stop_reason: Optional[str],
    duration_ms: int,
    tool_executions: list[dict[str, Any]],
) -> None:
    """Persist one completed invocation and its ordered, sanitized tool calls."""
    if status not in {"completed", "interrupted", "error"}:
        raise ValueError(f"Unsupported agent turn status: {status}")
    settings = _settings_for(AGENT_TURN_TABLE)
    turn_sql = f"""
        INSERT INTO {AGENT_TURN_TABLE} (
            turn_id, session_id, app_user, region, user_message_id, assistant_message_id,
            user_message, assistant_response, status, stop_reason, duration_ms
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (turn_id) DO UPDATE SET
            assistant_message_id = EXCLUDED.assistant_message_id,
            assistant_response = EXCLUDED.assistant_response,
            status = EXCLUDED.status,
            stop_reason = EXCLUDED.stop_reason,
            duration_ms = EXCLUDED.duration_ms,
            completed_at = now()
    """
    tool_sql = f"""
        INSERT INTO {TOOL_EXECUTION_TABLE} (
            execution_id, turn_id, sequence_number, tool_name, tool_input, status
        ) VALUES %s
        ON CONFLICT (execution_id) DO UPDATE SET
            tool_input = EXCLUDED.tool_input,
            status = EXCLUDED.status
    """
    with connect(settings) as conn:
        with conn.cursor() as cur:
            cur.execute(
                turn_sql,
                (
                    turn_id,
                    session_id,
                    app_user,
                    (region or "apac").strip().lower(),
                    user_message_id,
                    assistant_message_id,
                    user_message,
                    assistant_response,
                    status,
                    stop_reason,
                    max(0, int(duration_ms)),
                ),
            )
            if tool_executions:
                values = [
                    (
                        item["execution_id"],
                        turn_id,
                        sequence,
                        item["tool_name"],
                        json.dumps(item.get("tool_input") or {}, default=str),
                        item.get("status") or "completed",
                    )
                    for sequence, item in enumerate(tool_executions, start=1)
                ]
                extras.execute_values(cur, tool_sql, values)
        conn.commit()


def record_response_feedback(
    *,
    feedback_id: str,
    assistant_message_id: str,
    app_user: str,
    vote: int,
    comment: Optional[str] = None,
) -> None:
    """Upsert one user's vote for an owned assistant response."""
    if vote not in {-1, 1}:
        raise ValueError("vote must be either -1 or 1")
    settings = _settings_for(RESPONSE_FEEDBACK_TABLE)
    sql = f"""
        INSERT INTO {RESPONSE_FEEDBACK_TABLE} (
            feedback_id, assistant_message_id, turn_id, session_id, app_user, vote, comment
        )
        SELECT %s, turn.assistant_message_id, turn.turn_id, turn.session_id, turn.app_user, %s, %s
        FROM {AGENT_TURN_TABLE} AS turn
        WHERE turn.assistant_message_id = %s AND turn.app_user = %s
        ON CONFLICT (app_user, assistant_message_id) DO UPDATE SET
            vote = EXCLUDED.vote,
            comment = EXCLUDED.comment,
            updated_at = now()
        RETURNING feedback_id
    """
    normalized_comment = (comment or "").strip() or None
    with connect(settings) as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (feedback_id, vote, normalized_comment, assistant_message_id, app_user))
            saved = cur.fetchone()
        conn.commit()
    if not saved:
        raise ValueError("The selected response was not found for this user.")


def record_unresolved_feedback(
    *,
    feedback_id: str,
    app_user: str,
    vote: int,
    response_content: str,
    comment: Optional[str] = None,
) -> None:
    """Store feedback for a legacy or welcome response without a normalized turn ID."""
    if vote not in {-1, 1}:
        raise ValueError("vote must be either -1 or 1")
    settings = _settings_for(UNRESOLVED_FEEDBACK_TABLE)
    sql = f"""
        INSERT INTO {UNRESOLVED_FEEDBACK_TABLE} (
            feedback_id, app_user, vote, comment, response_content
        ) VALUES (%s, %s, %s, %s, %s)
    """
    with connect(settings) as conn:
        with conn.cursor() as cur:
            cur.execute(
                sql,
                (
                    feedback_id,
                    app_user,
                    vote,
                    (comment or "").strip() or None,
                    response_content[:20000],
                ),
            )
        conn.commit()


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


def get_change_history(
    dataset: str,
    region: Optional[str] = None,
    change_category: Optional[str] = None,
    since_days: Optional[int] = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    """Return change-history rows for one dataset, newest first. Optional filters on
    region, change_category, and how far back to look (in days)."""
    if not dataset:
        raise ValueError("dataset is required to read change history.")
    where = ["dataset = %s"]
    params: list[Any] = [dataset]
    if region:
        where.append("region = %s")
        params.append(region)
    if change_category:
        where.append("change_category = %s")
        params.append(change_category)
    if since_days and since_days > 0:
        where.append("changed_at >= now() - make_interval(days => %s)")
        params.append(int(since_days))
    params.append(max(1, min(int(limit or 200), 1000)))

    sql = f"""
        SELECT id, dataset, region, session_id, dataset_category, change_category,
               change_item, change_from, change_to, change_reason, change_by, changed_at
        FROM {CHANGE_HISTORY_TABLE}
        WHERE {' AND '.join(where)}
        ORDER BY changed_at DESC, id DESC
        LIMIT %s
    """
    settings = _settings_for(CHANGE_HISTORY_TABLE)
    with connect(settings) as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            columns = [desc[0] for desc in cur.description]
            rows = cur.fetchall()
    return [dict(zip(columns, row)) for row in rows]


if __name__ == "__main__":
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass
    logging.basicConfig(level=logging.INFO)
    ensure_tables()
