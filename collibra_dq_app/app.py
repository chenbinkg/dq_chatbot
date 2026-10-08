"""
Local Gradio chatbot for J&J Collibra DQ super-users.

Same Agent + streaming UI pattern as ../unified_app/app.py, but:
- Runs entirely locally (no AWS Cognito/SSM) -- login is a simple username/
  password check against a SUPERUSER_CREDENTIALS allowlist env var.
- Uses JNJClaudeGatewayModel (J&J GenAI Gateway) instead of BedrockModel.
- Uses the local collibra_tools.py function tools instead of an MCP client,
  so agent actions call Collibra DQ REST APIs directly.

Run:
    export SUPERUSER_CREDENTIALS="alice:s3cret,bob:hunter2"
    export JNJ_GENAI_API_KEY=...
    export CDQ_BASE_URL_APAC=... CDQ_USERNAME_APAC=... CDQ_PASSWORD_APAC=...
    python app.py
"""

import asyncio
import atexit
import json
import logging
import os
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

import gradio as gr

import chat_store
from dq_agent import build_agent
from prompt_manager import PromptTemplateManager
from strands_agent import get_atlassian_mcp_client

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from postgres_io import build_settings, read_sql
from collibra_tools import CollibraDQClient, apply_pending_changes, list_pending_changes

_clients = {}

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

agent = None  # lazily built after successful login
agent_region = None
mcp_client = None
mcp_status = "Atlassian MCP has not been initialized."
prompt_manager = PromptTemplateManager()
_feedback_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="feedback-save")

BU_MAPPING_TABLE = os.getenv("DQM_BU_MAPPING_TABLE", "public.dqm_business_unit_mapping")

# Max fields the ask_user_for_input interrupt form renders (fixed Gradio component slots).
MAX_INTERRUPT_FIELDS = 4
_INTERRUPT_FORM_OUTPUT_COUNT = 3 + MAX_INTERRUPT_FIELDS * 4  # group + question + state, then rows/inputs/dropdowns/labels

def _get_client(region: str = "apac") -> CollibraDQClient:
    region = (region or "apac").strip().lower()
    if region not in _clients:
        base_url = os.getenv(f"CDQ_BASE_URL_{region.upper()}")
        username = os.getenv(f"CDQ_USERNAME_{region.upper()}")
        password = os.getenv(f"CDQ_PASSWORD_{region.upper()}")
        _clients[region] = CollibraDQClient(base_url=base_url, username=username, password=password, region=region)
    return _clients[region]

def fetch_dataset_names() -> list[str]:
    """Read the known dataset names from the business unit mapping table."""
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
        raise ValueError("DB_HOST/DB_NAME/DB_USER/DB_PASSWORD must be set to query dataset names.")
    # Table name comes from server-side config, never from user input.
    query = f"SELECT DISTINCT dataset FROM {BU_MAPPING_TABLE} WHERE dataset IS NOT NULL ORDER BY dataset"
    df = read_sql(query, settings)
    return [str(v) for v in df["dataset"].tolist() if str(v).strip()]



def _activity_message(
    title: str, content: str, status: str = "pending", message_id: str | None = None, parent_id: str | None = None
):
    metadata = {"title": title, "status": status}
    if message_id:
        metadata["id"] = message_id
    if parent_id:
        metadata["parent_id"] = parent_id
    return {"role": "assistant", "content": content, "metadata": metadata}


def _new_turn_trace(user_message: str) -> dict:
    return {
        "turn_id": f"turn-{uuid.uuid4().hex}",
        "user_message_id": f"message-{uuid.uuid4().hex}",
        "assistant_message_id": f"response-{uuid.uuid4().hex}",
        "user_message": user_message,
        "assistant_response": "",
        "started_at": time.monotonic(),
        "tool_executions": [],
    }


def _conversation_message(role: str, content: str, message_id: str) -> dict:
    return {"role": role, "content": content, "metadata": {"id": message_id}}


def _sanitized_tool_input(value):
    """Redact credential-like fields and cap strings before trace persistence."""
    if isinstance(value, dict):
        sanitized = {}
        for key, item in value.items():
            normalized_key = str(key).lower().replace("-", "_")
            if any(marker in normalized_key for marker in ("password", "secret", "token", "authorization", "api_key")):
                sanitized[key] = "[REDACTED]"
            else:
                sanitized[key] = _sanitized_tool_input(item)
        return sanitized
    if isinstance(value, list):
        return [_sanitized_tool_input(item) for item in value[:100]]
    if isinstance(value, str):
        return value[:4000]
    return value


def _persist_turn_trace(trace: dict, username: str, session_id: str, region: str, agent_result=None, error=None) -> None:
    stop_reason = getattr(agent_result, "stop_reason", None) if agent_result is not None else None
    if error is not None:
        status = "error"
        stop_reason = type(error).__name__
        for execution in trace["tool_executions"]:
            if execution["status"] != "completed":
                execution["status"] = "error"
    elif stop_reason == "interrupt":
        status = "interrupted"
    else:
        status = "completed"
    try:
        chat_store.log_agent_turn(
            turn_id=trace["turn_id"],
            session_id=session_id,
            app_user=username,
            region=region,
            user_message_id=trace["user_message_id"],
            assistant_message_id=trace["assistant_message_id"],
            user_message=trace["user_message"],
            assistant_response=trace["assistant_response"],
            status=status,
            stop_reason=stop_reason,
            duration_ms=int((time.monotonic() - trace["started_at"]) * 1000),
            tool_executions=trace["tool_executions"],
        )
    except Exception:
        logger.exception("Failed to persist agent trace for turn %s", trace["turn_id"])


def _tool_input_text(tool_input) -> str:
    if not tool_input:
        return "Waiting for tool input..."
    if isinstance(tool_input, str):
        text = tool_input
    else:
        text = json.dumps(tool_input, indent=2, default=str)
    return f"```json\n{text[:2000]}\n```"


def _display_chunks(text: str, size: int = 12):
    for start in range(0, len(text), size):
        yield text[start:start + size]


def _interrupt_field_type(field: dict) -> str:
    return (field or {}).get("type") or "text"


def _hidden_interrupt_form_updates():
    """Gradio updates that hide/clear the "agent needs input" form (ask_user_for_input)."""
    updates = [gr.update(visible=False), gr.update(value=""), None]
    updates += [gr.update(visible=False) for _ in range(MAX_INTERRUPT_FIELDS)]  # rows
    updates += [gr.update(visible=False, value="") for _ in range(MAX_INTERRUPT_FIELDS)]  # textboxes
    updates += [gr.update(visible=False, choices=[], value=None) for _ in range(MAX_INTERRUPT_FIELDS)]  # dropdowns
    updates += [gr.update(value="Field") for _ in range(MAX_INTERRUPT_FIELDS)]  # labels
    return updates


def _interrupt_form_updates(agent_result):
    """Build the Gradio updates for the "agent needs input" form from an AgentResult --
    populated with the ask_user_for_input tool's question/fields when the agent just
    paused on that interrupt, otherwise hidden."""
    interrupts = getattr(agent_result, "interrupts", None) if agent_result else None
    if not interrupts:
        return _hidden_interrupt_form_updates()

    interrupt = interrupts[0]
    reason = interrupt.reason or {}
    question = reason.get("question") or "The agent needs more information:"
    fields = (reason.get("fields") or [])[:MAX_INTERRUPT_FIELDS]
    if not fields:
        return _hidden_interrupt_form_updates()

    state = {"interrupt_id": interrupt.id, "fields": fields}
    row_updates, text_updates, dropdown_updates, label_updates = [], [], [], []
    for i in range(MAX_INTERRUPT_FIELDS):
        if i >= len(fields):
            row_updates.append(gr.update(visible=False))
            text_updates.append(gr.update(visible=False, value=""))
            dropdown_updates.append(gr.update(visible=False, choices=[], value=None))
            label_updates.append(gr.update(value="Field"))
            continue
        field = fields[i]
        label = field.get("label") or field.get("name") or f"Field {i + 1}"
        if field.get("required", True):
            label += " *"
        row_updates.append(gr.update(visible=True))
        label_updates.append(gr.update(value=f"**{label}**"))
        if _interrupt_field_type(field) == "choice":
            options = field.get("options") or []
            dropdown_updates.append(
                gr.update(visible=True, choices=options, value=field.get("default") or (options[0] if options else None))
            )
            text_updates.append(gr.update(visible=False, value=""))
        else:
            placeholder = "HH:MM:SS" if _interrupt_field_type(field) == "time" else ""
            text_updates.append(gr.update(visible=True, value=field.get("default") or "", placeholder=placeholder))
            dropdown_updates.append(gr.update(visible=False, choices=[], value=None))

    return (
        [gr.update(visible=True), gr.update(value=f"**{question}**"), state]
        + row_updates + text_updates + dropdown_updates + label_updates
    )


async def _drive_agent_stream(prompt, chat_history, invocation_index, verbose, result_holder, trace):
    """Consume one agent.stream_async(prompt) turn, mutating chat_history in place and
    yielding it after each meaningful event. `prompt` can be a new user message (str) or a
    list of interruptResponse content blocks to resume a paused ask_user_for_input call.
    Stashes the final AgentResult in result_holder['result'] so the caller can check
    result.stop_reason (=="interrupt" means the agent is waiting on ask_user_for_input)."""
    result_text = ""
    answer_index = None
    reasoning_index = None
    reasoning_text = ""
    tool_indices: dict[str, int] = {}
    active_tool_ids: set[str] = set()
    # All tool calls in this turn nest under one collapsed parent bubble (metadata
    # parent_id) instead of each getting its own top-level chat row -- with 10+ tool
    # calls in a turn, that was pushing the actual answer far down/out of view.
    tools_parent_id = f"tools-{invocation_index}"
    tools_parent_index: int | None = None

    def complete_active_tools():
        for tool_id in active_tool_ids:
            idx = tool_indices[tool_id]
            chat_history[idx]["metadata"]["status"] = "done"
            for execution in trace["tool_executions"]:
                if execution["execution_id"] == f"{trace['turn_id']}:{tool_id}":
                    execution["status"] = "completed"
                    break
        active_tool_ids.clear()
        if tools_parent_index is not None:
            chat_history[tools_parent_index]["metadata"]["status"] = "done"

    # Strands rejects a plain-text prompt while an interrupt is pending (user typed in the
    # chat box instead of using the form), so pass the text as the answer to every open interrupt.
    interrupt_state = getattr(agent, "_interrupt_state", None)
    if isinstance(prompt, str) and interrupt_state is not None and interrupt_state.activated:
        prompt = [
            {"interruptResponse": {"interruptId": interrupt_id, "response": {"free_text_reply": prompt}}}
            for interrupt_id, interrupt in interrupt_state.interrupts.items()
            if interrupt.response is None
        ] or prompt

    async for event in agent.stream_async(prompt):
        if event.get("init_event_loop"):
            chat_history[invocation_index]["content"] = "Model initialized."
            yield chat_history

        if event.get("start_event_loop"):
            complete_active_tools()
            chat_history[invocation_index]["content"] = "Evaluating next action..."
            yield chat_history

        if event.get("reasoning") and event.get("reasoningText"):
            reasoning_text += event["reasoningText"]
            # Update the top status line unconditionally -- this is also how the
            # model provider reports a J&J gateway cold-start retry in progress, and
            # that needs to stay visible even with "Show activity and reasoning" off,
            # otherwise a multi-minute retry backoff looks like the UI has hung.
            chat_history[invocation_index]["content"] = event["reasoningText"].strip() or chat_history[invocation_index]["content"]
            if verbose:
                if reasoning_index is None:
                    reasoning_index = len(chat_history)
                    chat_history.append(
                        _activity_message("Reasoning", reasoning_text, message_id="reasoning-trace")
                    )
                else:
                    chat_history[reasoning_index]["content"] = reasoning_text
            yield chat_history

        if "current_tool_use" in event:
            tool_use = event["current_tool_use"]
            tool_id = tool_use.get("toolUseId") or tool_use.get("name")
            tool_name = tool_use.get("name") or "Unknown tool"
            if tool_id:
                if tools_parent_index is None:
                    tools_parent_index = len(chat_history)
                    chat_history.append(_activity_message("🛠️ Tool calls", "", message_id=tools_parent_id))
                if tool_id not in tool_indices:
                    tool_indices[tool_id] = len(chat_history)
                    chat_history.append(
                        _activity_message(
                            tool_name,
                            _tool_input_text(tool_use.get("input")),
                            message_id=tool_id,
                            parent_id=tools_parent_id,
                        )
                    )
                    chat_history[tools_parent_index]["metadata"]["title"] = f"🛠️ Tool calls ({len(tool_indices)})"
                else:
                    chat_history[tool_indices[tool_id]]["content"] = _tool_input_text(
                        tool_use.get("input")
                    )
                active_tool_ids.add(tool_id)
                execution_id = f"{trace['turn_id']}:{tool_id}"
                existing_execution = next(
                    (item for item in trace["tool_executions"] if item["execution_id"] == execution_id),
                    None,
                )
                sanitized_input = _sanitized_tool_input(tool_use.get("input") or {})
                if existing_execution is None:
                    trace["tool_executions"].append(
                        {
                            "execution_id": execution_id,
                            "tool_name": tool_name,
                            "tool_input": sanitized_input,
                            "status": "error",
                        }
                    )
                else:
                    existing_execution["tool_input"] = sanitized_input
                chat_history[invocation_index]["content"] = f"Waiting for {tool_name}..."
                logger.info("Tool call: %s(%s)", tool_name, tool_use.get("input"))
                yield chat_history

        if "data" in event:
            complete_active_tools()
            chat_history[invocation_index]["content"] = "Writing response..."
            if answer_index is None:
                answer_index = len(chat_history)
                chat_history.append(
                    _conversation_message("assistant", "", trace["assistant_message_id"])
                )
            # The J&J gateway returns one complete text block, so chunk it for
            # progressive display even though this is not true token streaming.
            for chunk in _display_chunks(event["data"]):
                result_text += chunk
                chat_history[answer_index]["content"] = result_text
                yield chat_history
                await asyncio.sleep(0.005)

        if "result" in event:
            result_holder["result"] = event["result"]

    complete_active_tools()
    if reasoning_index is not None:
        chat_history[reasoning_index]["metadata"]["status"] = "done"
    agent_result = result_holder.get("result")
    if agent_result is not None and agent_result.stop_reason == "interrupt":
        chat_history[invocation_index]["content"] = "Waiting for your input below."
    else:
        chat_history[invocation_index]["content"] = "Completed."
        if answer_index is None:
            chat_history.append(
                _conversation_message(
                    "assistant",
                    result_text or "No response text returned.",
                    trace["assistant_message_id"],
                )
            )
    trace["assistant_response"] = result_text or "No response text returned."
    chat_history[invocation_index]["metadata"]["status"] = "done"
    yield chat_history


def _entry_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            block.get("text", "") if isinstance(block, dict) else str(block)
            for block in content
        )
    return ""


def _history_to_agent_messages(conversation_history: list[dict]) -> list[dict]:
    """Rebuild the Strands agent's own conversation log (plain user/assistant text turns)
    from the UI-oriented history saved in Postgres, dropping activity/tool/reasoning rows
    that only exist for display in the Gradio chatbot."""
    messages = []
    for entry in conversation_history or []:
        role = entry.get("role")
        if role not in ("user", "assistant"):
            continue
        metadata = entry.get("metadata") or {}
        if metadata.get("title") or metadata.get("status") or metadata.get("parent_id"):
            continue
        text = _entry_text(entry.get("content"))
        if not text:
            continue
        messages.append({"role": role, "content": [{"text": text}]})
    return messages


def select_response_feedback(chat_history: list[dict] | None, feedback: gr.LikeData):
    """Show the comment form while one response has active feedback selected."""
    message_index = feedback.index[0] if isinstance(feedback.index, tuple) else feedback.index
    if not isinstance(message_index, int) or not chat_history or not 0 <= message_index < len(chat_history):
        return gr.update(visible=False), None, "", "Feedback is unavailable for this response."

    message = chat_history[message_index]
    metadata = message.get("metadata") if isinstance(message, dict) else getattr(message, "metadata", None)
    if not isinstance(metadata, dict):
        metadata = dict(metadata or {})
    assistant_message_id = metadata.get("id")
    if not isinstance(assistant_message_id, str) or not assistant_message_id.startswith("response-"):
        group_start = message_index
        while group_start > 0 and chat_history[group_start - 1].get("role") == "assistant":
            group_start -= 1
        group_end = message_index + 1
        while group_end < len(chat_history) and chat_history[group_end].get("role") == "assistant":
            group_end += 1
        for candidate in reversed(chat_history[group_start:group_end]):
            candidate_metadata = candidate.get("metadata") or {}
            candidate_id = candidate_metadata.get("id")
            if isinstance(candidate_id, str) and candidate_id.startswith("response-"):
                assistant_message_id = candidate_id
                break
    if not isinstance(assistant_message_id, str) or not assistant_message_id.startswith("response-"):
        event_value = feedback.value.get("content") if isinstance(feedback.value, dict) else feedback.value
        event_text = _entry_text(event_value)
        for candidate in reversed(chat_history):
            if not isinstance(candidate, dict) or candidate.get("role") != "assistant":
                continue
            candidate_metadata = candidate.get("metadata") or {}
            candidate_id = candidate_metadata.get("id")
            if (
                isinstance(candidate_id, str)
                and candidate_id.startswith("response-")
                and _entry_text(candidate.get("content")) == event_text
            ):
                assistant_message_id = candidate_id
                break
    if not isinstance(assistant_message_id, str) or not assistant_message_id.startswith("response-"):
        assistant_message_id = None

    liked = feedback.liked
    if liked is True or str(liked).strip().lower() in {"like", "liked", "upvote", "thumbs up"}:
        vote = 1
        label = "upvote"
    elif liked is False or str(liked).strip().lower() in {"dislike", "disliked", "downvote", "thumbs down"}:
        vote = -1
        label = "downvote"
    else:
        return gr.update(visible=False), None, "", ""

    selection = {
        "assistant_message_id": assistant_message_id,
        "vote": vote,
        "label": label,
        "response_content": _entry_text(feedback.value),
    }
    return gr.update(visible=True), selection, "", ""


def _persist_response_feedback(comment: str, username: str, selection: dict) -> None:
    """Write one feedback record; callers choose whether to wait for completion."""
    if selection.get("assistant_message_id"):
        chat_store.record_response_feedback(
            feedback_id=f"feedback-{uuid.uuid4().hex}",
            assistant_message_id=selection["assistant_message_id"],
            app_user=username,
            vote=selection["vote"],
            comment=comment,
        )
    else:
        chat_store.record_unresolved_feedback(
            feedback_id=f"feedback-{uuid.uuid4().hex}",
            app_user=username,
            vote=selection["vote"],
            comment=comment,
            response_content=selection["response_content"],
        )


def _persist_cancelled_feedback(username: str, selection: dict) -> None:
    try:
        _persist_response_feedback("", username, selection)
    except Exception:
        logger.exception("Failed to save commentless feedback for response %s", selection.get("assistant_message_id"))


def submit_response_feedback(comment: str, username: str, selection: dict | None):
    """Persist feedback synchronously so explicit comment submissions report errors."""
    if not selection:
        return "Select Like or Dislike on a response first.", gr.update(visible=False), "", None
    if not username:
        return "Please log in before submitting feedback.", gr.update(visible=True), comment, selection

    try:
        _persist_response_feedback(comment, username, selection)
    except Exception:
        logger.exception("Failed to save feedback for response %s", selection.get("assistant_message_id"))
        return "Feedback could not be saved. Please try again.", gr.update(visible=True), comment, selection
    return f"Thanks. Your {selection['label']} was saved.", gr.update(visible=False), "", None


def cancel_feedback_comment(username: str, selection: dict | None):
    """Dismiss immediately, saving the selected vote without a comment in the background."""
    if not selection:
        return "", gr.update(visible=False), "", None
    if not username:
        return "Please log in before submitting feedback.", gr.update(visible=True), "", selection

    _feedback_executor.submit(_persist_cancelled_feedback, username, dict(selection))
    return f"Thanks. Your {selection['label']} was saved.", gr.update(visible=False), "", None


def _build_chat_agent(region: str = "apac"):
    """Build the persistent chat agent with local tools and Atlassian MCP tools."""
    global mcp_client, mcp_status, agent_region
    region = (region or "apac").strip().lower()
    try:
        mcp_client = get_atlassian_mcp_client()
        mcp_status = "Atlassian MCP tools are configured; Jira credentials will be validated on the first tool call."
        agent_region = region
        return build_agent(mcp_clients=[mcp_client], region=region)
    except Exception as exc:
        logger.warning("Atlassian MCP unavailable; starting with local Collibra tools only: %s", exc)
        mcp_status = f"Atlassian MCP unavailable; using local Collibra tools only ({exc})."
        agent_region = region
        return build_agent(region=region)


def _ensure_chat_agent(region: str = "apac"):
    global agent, agent_region
    region = (region or "apac").strip().lower()
    if agent is not None and agent_region != region:
        agent.cleanup()
        agent = None
        agent_region = None
    if agent is None:
        agent = _build_chat_agent(region)
    return agent


def _cleanup_agent():
    if agent is not None:
        agent.cleanup()


atexit.register(_cleanup_agent)


def _load_superuser_allowlist() -> dict[str, str]:
    raw = os.getenv("SUPERUSER_CREDENTIALS", "")
    allowlist = {}
    for pair in raw.split(","):
        pair = pair.strip()
        if not pair or ":" not in pair:
            continue
        user, pwd = pair.split(":", 1)
        allowlist[user.strip()] = pwd
    return allowlist


# def authenticate(username: str, password: str):
#     """Check username/password against the SUPERUSER_CREDENTIALS allowlist."""
#     allowlist = _load_superuser_allowlist()
#     if not allowlist:
#         return "Login disabled: SUPERUSER_CREDENTIALS is not configured on the server.", False, ""
#     if allowlist.get(username) == password and password:
#         return f"Logged in as {username}.", True, username
#     return "Invalid username or password.", False, ""


def authenticate(region: str, username: str, password: str):
    """Check credentials and capture the selected Collibra region."""
    region = (region or "apac").strip().lower()

    if region not in {"apac", "cn"}:
        return "Invalid region selected.", False, "", region

    allowlist = _load_superuser_allowlist()

    if not allowlist:
        return (
            "Login disabled: SUPERUSER_CREDENTIALS is not configured "
            "on the server.",
            False,
            "",
            region,
        )

    if allowlist.get(username) == password and password:
        # Validate that the region configuration exists.
        _get_client(region)

        return (
            f"Logged in as {username} using the {region.upper()} region.",
            True,
            username,
            region,
        )

    return "Invalid username or password.", False, "", region


def _welcome_message(region: str) -> str:
    return (
        "👋 Welcome! I'm your **Data Quality Automation Assistant** for J&J's Collibra DQ "
        f"platform — here to help you inspect, manage, and improve your DQ datasets in the "
        f"{region.upper()} region.\n\n"
        "Here's what I can help you with:\n\n"
        "- 🔍 **Inspect** dataset definitions, custom rules, alerts, and business unit mappings\n"
        "- ✏️ **Modify** dataset configurations — metaTags, schedules, profile settings, custom/layer rules, and alerts\n"
        "- 🆕 **Create** new DQ datasets backed by Redshift or S3\n"
        "- 🔬 **Investigate** DQ findings, adaptive rule breaks, and delta profiles\n"
        "- 🎫 **Submit** Jira DQ change request or new set-up tickets\n\n"
        "What would you like to do today?"
    )


def _start_fresh_conversation(region: str) -> list[dict]:
    """Seed the agent with the welcome exchange shown in the UI and return the chat history."""
    region = (region or "apac").strip().lower()
    welcome = _welcome_message(region)
    # The J&J gateway's input guardrail refuses a lone first user turn; any prior exchange avoids it.
    _ensure_chat_agent(region).messages = [
        {"role": "user", "content": [{"text": "Hi"}]},
        {"role": "assistant", "content": [{"text": welcome}]},
    ]
    return [{"role": "assistant", "content": welcome}]


def check_resume(username: str, region: str):
    """After login, offer to resume this user's most recent chat session if one exists,
    otherwise start a fresh conversation with the welcome message."""
    region = (region or "apac").strip().lower()
    if not username:
        return gr.update(visible=False), gr.update(visible=False), None, gr.update(), gr.update(visible=False)
    try:
        latest = chat_store.get_latest_session(username, region=region)
    except Exception:
        logger.exception("Failed to check for resumable chat history for user %s in region %s", username, region)
        latest = None
    if not latest or not latest.get("conversation_history"):
        return (
            gr.update(visible=False), gr.update(visible=False), None,
            _start_fresh_conversation(region), gr.update(visible=True),
        )
    message = (
        f"You have a previous {region.upper()} conversation from {latest['updated_at']} "
        f"({latest['turn_count']} messages). Resume it?"
    )
    return gr.update(value=message, visible=True), gr.update(visible=True), latest, gr.update(), gr.update(visible=False)


def resume_previous_chat(latest: dict | None):
    if not latest:
        return [], uuid.uuid4().hex, gr.update(visible=False), gr.update(visible=False), gr.update(visible=False)
    _ensure_chat_agent(latest.get("region") or "apac")
    agent.messages = _history_to_agent_messages(latest["conversation_history"])
    return (
        latest["conversation_history"], latest["session_id"],
        gr.update(visible=False), gr.update(visible=False), gr.update(visible=False),
    )


def dismiss_resume_prompt(region: str):
    return gr.update(visible=False), gr.update(visible=False), _start_fresh_conversation(region), gr.update(visible=True)


def refresh_pending_changes(username: str, session_id: str, region: str, previous_mapping: dict | None = None):
    """Re-read this session's pending propose_* change_ids for the approval panel."""
    if not username:
        return gr.update(choices=[], value=[]), {}, gr.update()
    chat_store.set_session_context(username, session_id, region or "apac")
    try:
        result = list_pending_changes()
    except Exception:
        logger.exception("Failed to list pending changes for session %s", session_id)
        return gr.update(choices=[], value=[]), {}, gr.update()
    mapping = {c["label"]: c["change_id"] for c in result.get("pending_changes", [])}
    # Expand only when a new change_id shows up (so a fresh proposal isn't missed), collapse when nothing is pending.
    new_ids = set(mapping.values()) - set((previous_mapping or {}).values())
    if not mapping:
        accordion_update = gr.update(open=False)
    elif new_ids:
        accordion_update = gr.update(open=True)
    else:
        accordion_update = gr.update()
    return gr.update(choices=list(mapping.keys()), value=[]), mapping, accordion_update


async def apply_selected_pending_changes(
    selected_labels: list[str],
    label_to_id: dict,
    reason: str,
    chat_history,
    verbose: bool,
    username: str,
    session_id: str,
    region: str,
):
    """Approve button handler: applies the checked pending changes directly, bypassing the
    LLM's own judgement of whether the user has confirmed, then tells the agent what was
    applied so it can continue the workflow (e.g. move on to custom rules) without the
    user having to re-explain it in the message box."""
    chat_history = list(chat_history or [])
    if not username:
        yield "Please log in first.", gr.update(), {}, gr.update(), chat_history, *([gr.skip()] * _INTERRUPT_FORM_OUTPUT_COUNT)
        return
    if not selected_labels:
        yield "No pending changes selected.", gr.update(), label_to_id, gr.update(), chat_history, *([gr.skip()] * _INTERRUPT_FORM_OUTPUT_COUNT)
        return
    if not reason or not reason.strip():
        yield (
            "Please enter a reason for the change before approving.",
            gr.update(), label_to_id, gr.update(), chat_history,
            *([gr.skip()] * _INTERRUPT_FORM_OUTPUT_COUNT),
        )
        return

    region = (region or "apac").strip().lower()
    chat_store.set_session_context(username, session_id, region)
    change_ids = [label_to_id[label] for label in selected_labels if label in label_to_id]
    try:
        result = apply_pending_changes(change_ids=change_ids, change_reason=reason.strip())
    except Exception as exc:
        logger.exception("Failed to apply pending changes %s", change_ids)
        status = f"Failed to apply changes: {exc}"
    else:
        applied = [r["change_id"] for r in result.get("results", []) if r.get("status") == "applied"]
        failed = [r for r in result.get("results", []) if r.get("status") == "error"]
        status = f"Applied {len(applied)}/{len(change_ids)} change(s)."
        if failed:
            status += f" Stopped on {failed[0]['change_id']}: {failed[0]['error']}"
        for r in result.get("results", []):
            job_error = (r.get("outcome") or {}).get("job_run_error")
            if job_error:
                status += f" Change {r['change_id']} was saved but the DQ job run could not be triggered: {job_error}"

    choices_update, new_mapping, _ = refresh_pending_changes(username, session_id, region)
    yield status, choices_update, new_mapping, gr.update(open=False), chat_history, *([gr.skip()] * _INTERRUPT_FORM_OUTPUT_COUNT)

    # Approving here never goes through the model, so it has no idea this happened --
    # nudge it with what was applied so it can pick the workflow back up on its own.
    nudge = (
        "I approved and applied the following change(s) via the Pending Approvals panel "
        f"(not through chat), with reason: \"{reason.strip()}\".\n"
        + "\n".join(f"- {label}" for label in selected_labels)
        + f"\n\nResult: {status}\n\n"
        "Continue the workflow from here if applicable, or confirm we're done."
    )
    trace = _new_turn_trace(nudge)
    chat_history.append(_conversation_message("user", nudge, trace["user_message_id"]))
    invocation_index = len(chat_history)
    chat_history.append(_activity_message("Agent", "Resuming...", message_id="agent-status"))
    yield status, gr.skip(), gr.skip(), gr.skip(), chat_history, *([gr.skip()] * _INTERRUPT_FORM_OUTPUT_COUNT)

    global agent
    agent = _ensure_chat_agent(region)
    result_holder: dict = {}
    try:
        async for updated_history in _drive_agent_stream(
            nudge, chat_history, invocation_index, verbose, result_holder, trace
        ):
            yield status, gr.skip(), gr.skip(), gr.skip(), updated_history, *([gr.skip()] * _INTERRUPT_FORM_OUTPUT_COUNT)
    except Exception as e:
        logger.exception("Agent error resuming after panel approval")
        chat_history[invocation_index]["content"] = "Failed."
        chat_history[invocation_index]["metadata"]["status"] = "done"
        error_text = f"Error: {e}"
        trace["assistant_response"] = error_text
        chat_history.append(_conversation_message("assistant", error_text, trace["assistant_message_id"]))
        _persist_turn_trace(trace, username, session_id, region, error=e)
        yield status, gr.skip(), gr.skip(), gr.skip(), chat_history, *_hidden_interrupt_form_updates()
        return

    _persist_turn_trace(trace, username, session_id, region, result_holder.get("result"))
    try:
        chat_store.log_chat_turn(username, session_id, region, chat_history)
    except Exception:
        logger.exception("Failed to persist chat history for session %s", session_id)

    # The agent may have proposed further changes (e.g. custom rules) while continuing.
    choices_update2, new_mapping2, accordion_update2 = refresh_pending_changes(username, session_id, region, new_mapping)
    yield (
        status, choices_update2, new_mapping2, accordion_update2, chat_history,
        *_interrupt_form_updates(result_holder.get("result")),
    )


def get_prompt_categories():
    """Return list of prompt categories for dropdown."""
    return prompt_manager.get_categories()


def get_prompts_for_category(category: str):
    """Return prompts in a given category."""
    if not category:
        return []
    templates = prompt_manager.get_templates_by_category(category)
    return [f"{t.title} - {t.description}" for t in templates]


# Anchors the Submit button inside the message textbox, bottom-right.
APP_CSS = """
/* Only the inner message list should scroll; the outer block scrolling shows a second, bottomless scrollbar. */
#chat_conversation {
    overflow: hidden !important;
}
#msg_box_wrap {
    position: relative;
}
#msg_box_wrap #msg_send_btn {
    position: absolute;
    right: 28px;
    bottom: 14px;
    z-index: 10;
    width: auto;
    min-width: 110px;
    height: 40px;
    font-size: 15px;
    flex: none;
}
#msg_box_wrap textarea {
    resize: none;
    padding-bottom: 60px;
}
.field-query-btn {
    align-self: center;
    height: 40px;
    min-width: 96px;
    border-radius: 8px;
    font-size: 14px;
    white-space: nowrap;
}
"""

# The multi-line message box swallows Enter, so bind it to the Submit button
# (Shift+Enter still inserts a newline).
ENTER_TO_SUBMIT_JS = """
() => {
    const bind = () => {
        const textarea = document.querySelector('#msg_box_wrap textarea');
        if (!textarea || textarea.dataset.enterSubmitBound) return;
        textarea.dataset.enterSubmitBound = '1';
        textarea.addEventListener('keydown', (event) => {
            if (event.key !== 'Enter' || event.shiftKey || event.isComposing) return;
            event.preventDefault();
            event.stopPropagation();
            const btn = document.querySelector('#msg_send_btn');
            if (btn && !btn.disabled) btn.click();
        }, true);
    };
    bind();
    new MutationObserver(bind).observe(document.body, { childList: true, subtree: true });
}
"""

# Fires immediately on click/submit (registered as its own no-op listener, independent of
# the streaming response) so the page scrolls right away instead of waiting for the whole
# agent turn to finish. Targets the (now-collapsed, so small) Quick Prompts accordion
# rather than the chatbot itself, so that bar stays in view above the conversation too.
SCROLL_TO_CHAT_JS = """
() => {
    document.querySelector('#quick_prompts_accordion')?.scrollIntoView({ behavior: 'smooth', block: 'start' });
}
"""


with gr.Blocks(title="Data Quality AI Assistant") as demo:
    gr.Markdown("# Data Quality AI Assistant")
    gr.Markdown("One-stop platform for DQ management (Disclaimer: This application uses AI to assist with content generation).")

    with gr.Tabs() as tabs:
        with gr.Tab("Login", id="login_tab"):
            region_input = gr.Dropdown(
                choices=["apac", "cn"],
                label="Region",
                value="apac", # default
                interactive=True
            )
            region_state = gr.State("apac") # store region state in login, used throughout the session
    
            username_input = gr.Textbox(label="Username")
            password_input = gr.Textbox(label="Password", type="password")
            login_button = gr.Button("Login")
            login_output = gr.Textbox(label="Status", interactive=False)
            auth_state = gr.State(False)
            username_state = gr.State("")
            # One session_id per browser tab, reused for every turn so chat history
            # upserts into a single row and change history can be traced back to it.
            session_id_state = gr.State(lambda: uuid.uuid4().hex)
            # Holds the fetched previous session (session_id + conversation_history) between
            # login and the user's resume/start-fresh choice.
            resumable_history_state = gr.State(None)
    
        with gr.Tab("Chat", id="chat_tab"):
            resume_banner = gr.Markdown(visible=False)
            with gr.Row(visible=False) as resume_row:
                resume_yes_btn = gr.Button("Resume previous conversation")
                resume_no_btn = gr.Button("Start fresh")
            
            # --- Prompt Template Selector Section ---
            with gr.Accordion("Quick Prompts", open=False, elem_id="quick_prompts_accordion"):
                gr.Markdown("Select a pre-configured prompt to get started, or type a custom message below.")
    
                with gr.Row():
                    # Get all categories and set default to "Create Dataset"
                    all_categories = get_prompt_categories()
                    default_category = "Create Dataset" if "Create Dataset" in all_categories else (all_categories[0] if all_categories else None)
    
                    prompt_category = gr.Dropdown(
                        choices=all_categories,
                        value=default_category,
                        label="Category",
                        interactive=True
                    )
                    prompt_title = gr.Dropdown(
                        label="Prompt",
                        interactive=True,
                        allow_custom_value=True
                    )
    
                # State to hold template and field type info
                current_template_state = gr.State(None)
                field_types_state = gr.State([])  # Store field types for each field
    
                with gr.Group(visible=False) as prompt_form_group:
                    prompt_form_title = gr.Markdown(value="### Configure")
    
                    # Create up to 8 input fields (will be shown/hidden dynamically)
                    # We create both Textbox and Dropdown for each field, showing only the appropriate one
                    prompt_field_inputs = []  # Textboxes
                    prompt_field_dropdowns = []  # Dropdowns
                    prompt_field_labels = []
                    prompt_field_rows = []
                    prompt_field_query_btns = []
                    for i in range(8):
                        with gr.Row(visible=False, equal_height=True) as field_row:
                            field_label = gr.Markdown(value="Field", scale=2, min_width=220)
                            field_input = gr.Textbox(
                                show_label=False,
                                placeholder="",
                                lines=1,
                                scale=5,
                                visible=True
                            )
                            field_dropdown = gr.Dropdown(
                                choices=[],
                                show_label=False,
                                scale=5,
                                visible=False,
                                allow_custom_value=True
                            )
                            field_query_btn = gr.Button(
                                "Query",
                                variant="primary",
                                size="sm",
                                visible=False,
                                scale=1,
                                min_width=100,
                                elem_classes="field-query-btn",
                            )
                            prompt_field_inputs.append(field_input)
                            prompt_field_dropdowns.append(field_dropdown)
                            prompt_field_labels.append(field_label)
                            prompt_field_rows.append(field_row)
                            prompt_field_query_btns.append(field_query_btn)
    
                # Actions stay visible so a user can submit free-text typed into the Prompt box.
                with gr.Row():
                    prompt_submit = gr.Button("Use This Prompt", variant="primary", interactive=False)
                    prompt_clear_form = gr.Button("Clear Form")
            
            # Callback: populate prompts when category selected
            def on_category_change(category):
                prompts = get_prompts_for_category(category) if category else []
                choices_list = prompts if prompts else []
                value = choices_list[0] if len(choices_list) == 1 else None
    
                updates = [
                    gr.update(choices=choices_list, value=value),
                    gr.update(visible=False),
                    "### Configure",
                    gr.update(interactive=bool(value)),
                ]
                updates.extend([gr.update(visible=False) for _ in range(8)])  # field_rows
                updates.extend([gr.update(visible=False, value="") for _ in range(8)])  # field_inputs
                updates.extend([gr.update(visible=False, choices=[], value=None) for _ in range(8)])  # field_dropdowns
                updates.extend([gr.update(value="Field") for _ in range(8)])  # field_labels
                updates.extend([gr.update(visible=False) for _ in range(8)])  # field_query_btns
                return tuple(updates)
            
            prompt_category.change(
                on_category_change,
                [prompt_category],
                [prompt_title, prompt_form_group, prompt_form_title, prompt_submit] + prompt_field_rows + prompt_field_inputs + prompt_field_dropdowns + prompt_field_labels + prompt_field_query_btns
            )
            
            # Callback: render form when prompt selected
            def on_prompt_change(category, prompt_title_str):
                template = None
                if category and prompt_title_str:
                    for t in prompt_manager.get_templates_by_category(category):
                        if f"{t.title} - {t.description}" == prompt_title_str:
                            template = t
                            break
    
                if not template:
                    # No matching template: either nothing selected, or free text the user typed.
                    # Free text is itself a usable prompt, so keep the submit button enabled.
                    has_custom_text = bool(prompt_title_str and prompt_title_str.strip())
                    updates = [
                        None,
                        "### Configure",
                        gr.update(visible=False),
                        [],
                        gr.update(interactive=has_custom_text),
                    ]
                    updates.extend([gr.update(visible=False) for _ in range(8)])  # field_rows
                    updates.extend([gr.update(visible=False, value="") for _ in range(8)])  # field_inputs
                    updates.extend([gr.update(visible=False, choices=[], value=None) for _ in range(8)])  # field_dropdowns
                    updates.extend([gr.update(value="Field") for _ in range(8)])  # field_labels
                    updates.extend([gr.update(visible=False) for _ in range(8)])  # field_query_btns
                    return tuple(updates)
    
                form_title = f"### {template.title}"
                updates = [template, form_title, gr.update(visible=True)]
                
                # Build field updates in the correct order: all rows first, then all inputs, then all dropdowns, then all labels
                fields = template.get_all_fields()
                field_types = []
                
                # Add field_types tracking
                for i in range(8):
                    if i < len(fields):
                        field_types.append(fields[i].get('type', 'text'))
                    else:
                        field_types.append('text')
                updates.append(field_types)
                
                # Button starts disabled until all required fields are filled
                updates.append(gr.update(interactive=not template.get_required_fields()))
                
                # Add all field_row updates first
                for i in range(8):
                    if i < len(fields):
                        updates.append(gr.update(visible=True))
                    else:
                        updates.append(gr.update(visible=False))
                
                # Add all field_input updates (textbox)
                for i in range(8):
                    if i < len(fields):
                        field_def = fields[i]
                        field_type = field_def.get('type', 'text')
                        placeholder = field_def.get('placeholder', '')
                        lines = 3 if field_type == 'textarea' else 1
                        # Show textbox if not dropdown type
                        visible = field_type != 'dropdown'
                        updates.append(gr.update(visible=visible, value="", placeholder=placeholder, lines=lines))
                    else:
                        updates.append(gr.update(visible=False, value=""))
                
                # Add all field_dropdown updates
                for i in range(8):
                    if i < len(fields):
                        field_def = fields[i]
                        field_type = field_def.get('type', 'text')
                        # Show dropdown if type is dropdown
                        visible = field_type == 'dropdown'
                        choices = field_def.get('options', []) if visible else []
                        updates.append(gr.update(visible=visible, choices=choices, value=None))
                    else:
                        updates.append(gr.update(visible=False, choices=[], value=None))
                
                for i in range(8):
                    if i < len(fields):
                        field_def = fields[i]
                        label = f"{field_def['label']}{'*' if field_def.get('required') else ''}"
                        updates.append(gr.update(value=f"**{label}**"))
                    else:
                        updates.append(gr.update(value="Field"))
                
                for i in range(8):
                    has_lookup = i < len(fields) and bool(fields[i].get("lookup"))
                    updates.append(gr.update(visible=has_lookup))
                
                return tuple(updates)
            
            prompt_title.change(
                on_prompt_change,
                [prompt_category, prompt_title],
                [current_template_state, prompt_form_title, prompt_form_group, field_types_state, prompt_submit] + prompt_field_rows + prompt_field_inputs + prompt_field_dropdowns + prompt_field_labels + prompt_field_query_btns
            )
            
            # def on_query_datasets():
            #     try:
            #         # names = fetch_dataset_names()
            #         names = _get_client(region_state.value).list_datasets()
            #     except Exception as exc:
            #         logger.exception("Failed to load dataset names")
            #         raise gr.Error(f"Could not load dataset names: {exc}") from exc
            #     if not names:
            #         gr.Warning("No datasets found in the business unit mapping table.")
            #     return gr.update(choices=names)
    
            def on_query_datasets(region: str):
                try:
                    names = _get_client(region).list_datasets()
                except Exception as exc:
                    logger.exception(
                        "Failed to load dataset names for region %s",
                        region,
                    )
                    raise gr.Error(
                        f"Could not load dataset names for region '{region}': {exc}"
                    ) from exc
                if not names:
                    gr.Warning(f"No datasets found in the '{region}' region.")
                return gr.update(choices=names, value=None)
            
            for field_query_btn, field_dropdown in zip(prompt_field_query_btns, prompt_field_dropdowns):
                field_query_btn.click(
                    fn=on_query_datasets, 
                    inputs=[region_state], 
                    outputs=[field_dropdown]
                    )
            
            # --- End Prompt Template Selector Section ---
            
            # gradio 6.x dropped the `type=` kwarg -- messages format is now the only format.
            chatbot = gr.Chatbot(height=600, elem_id="chat_conversation", like_user_message=False)
            feedback_selection_state = gr.State(None)
            with gr.Row(visible=False) as feedback_form_group:
                feedback_comment = gr.Textbox(
                    label="Optional feedback comment",
                    placeholder="What was helpful or what should be improved?",
                    lines=1,
                    interactive=True,
                    scale=4,
                )
                feedback_submit = gr.Button("Submit feedback", variant="primary", scale=1)
                feedback_cancel = gr.Button("Cancel", scale=1)
            feedback_status = gr.Markdown()

            with gr.Row(visible=False) as starter_prompts_row:
                starter_btn_1 = gr.Button("🆕 Create a new dataset", size="sm")
                starter_btn_2 = gr.Button("🔍 Inspect a dataset", size="sm")
                starter_btn_3 = gr.Button("✏️ Modify a dataset", size="sm")
                starter_btn_4 = gr.Button("📊 Investigate DQ findings", size="sm")
                starter_btn_5 = gr.Button("🎫 Submit a Jira ticket", size="sm")
    
            with gr.Accordion("Pending approvals", open=False) as pending_accordion:
                gr.Markdown(
                    "Changes the agent has proposed (propose_new_dq_dataset, "
                    "propose_business_unit_assignment, propose_rule_change, etc.) but not yet "
                    "applied. Select one or more and approve them here instead of typing "
                    "\"yes\" in chat -- this is what actually writes to Collibra DQ."
                )
                pending_label_to_id = gr.State({})
                pending_checkboxes = gr.CheckboxGroup(label="Pending changes", choices=[])
                with gr.Row():
                    pending_reason = gr.Textbox(label="Reason for change", scale=3)
                    pending_refresh_btn = gr.Button("Refresh", scale=1)
                    pending_apply_btn = gr.Button("Approve selected", variant="primary", scale=1)
                pending_status = gr.Markdown()
    
            with gr.Group(visible=False) as interrupt_form_group:
                interrupt_question_md = gr.Markdown()
                interrupt_state = gr.State(None)
                interrupt_field_rows = []
                interrupt_field_inputs = []
                interrupt_field_dropdowns = []
                interrupt_field_labels = []
                for _i in range(MAX_INTERRUPT_FIELDS):
                    # Long/wrappy labels don't fit a side-by-side row, so stack label above control.
                    with gr.Column(visible=False) as _field_row:
                        _field_label = gr.Markdown(value="Field")
                        _field_input = gr.Textbox(show_label=False, visible=True, container=False)
                        _field_dropdown = gr.Dropdown(show_label=False, visible=False, container=False)
                        interrupt_field_rows.append(_field_row)
                        interrupt_field_inputs.append(_field_input)
                        interrupt_field_dropdowns.append(_field_dropdown)
                        interrupt_field_labels.append(_field_label)
                interrupt_submit_btn = gr.Button("Submit answers", variant="primary")
    
            interrupt_form_outputs = [
                interrupt_form_group,
                interrupt_question_md,
                interrupt_state,
                *interrupt_field_rows,
                *interrupt_field_inputs,
                *interrupt_field_dropdowns,
                *interrupt_field_labels,
            ]
    
            with gr.Group(elem_id="msg_box_wrap"):
                msg = gr.Textbox(
                    label="Message",
                    placeholder="e.g. What business unit is ds_conn_s3_x mapped to?",
                    lines=4,
                    max_lines=4,
                )
                send_button = gr.Button(
                    "Submit", variant="primary", interactive=False, size="sm", elem_id="msg_send_btn"
                )
            msg.change(
                lambda text: gr.update(interactive=bool(text and text.strip())),
                [msg],
                [send_button],
                queue=False,
            )
            verbose_mode = gr.Checkbox(label="Show activity and reasoning", value=True)
            clear = gr.Button("Clear")

            chatbot.like(
                select_response_feedback,
                inputs=[chatbot],
                outputs=[feedback_form_group, feedback_selection_state, feedback_comment, feedback_status],
                queue=False,
            )
            feedback_submit.click(
                submit_response_feedback,
                inputs=[feedback_comment, username_state, feedback_selection_state],
                outputs=[feedback_status, feedback_form_group, feedback_comment, feedback_selection_state],
                queue=False,
            )
            feedback_cancel.click(
                cancel_feedback_comment,
                inputs=[username_state, feedback_selection_state],
                outputs=[feedback_status, feedback_form_group, feedback_comment, feedback_selection_state],
                queue=False,
            )
    
            async def respond_async(message, chat_history, is_authed, verbose, username, session_id, region):
                region = (region or "apac").strip().lower()
                logger.info(
                    "Processing request for user=%s, session=%s, region=%s",
                    username,
                    session_id,
                    region,
                )
    
                chat_history = list(chat_history or [])
                if not is_authed:
                    chat_history = chat_history + [{"role": "assistant", "content": "Please log in on the Login tab first."}]
                    yield (chat_history, "", *_hidden_interrupt_form_updates())
                    return
    
                chat_store.set_session_context(username, session_id, region)
                trace = _new_turn_trace(message)
                chat_history.append(_conversation_message("user", message, trace["user_message_id"]))
                invocation_index = len(chat_history)
                chat_history.append(_activity_message("Agent", "Preparing request...", message_id="agent-status"))
                yield (chat_history, "", *_hidden_interrupt_form_updates())
    
                global agent
                previous_agent = agent
                agent = _ensure_chat_agent(region)
                if previous_agent is None:
                    chat_history[invocation_index]["content"] = mcp_status
                    yield (chat_history, "", *([gr.skip()] * _INTERRUPT_FORM_OUTPUT_COUNT))
    
                result_holder: dict = {}
                try:
                    async for updated_history in _drive_agent_stream(
                        message, chat_history, invocation_index, verbose, result_holder, trace
                    ):
                        yield (updated_history, "", *([gr.skip()] * _INTERRUPT_FORM_OUTPUT_COUNT))
                except Exception as e:
                    logger.exception("Agent error")
                    chat_history[invocation_index]["content"] = "Failed."
                    chat_history[invocation_index]["metadata"]["status"] = "done"
                    error_text = f"Error: {e}"
                    trace["assistant_response"] = error_text
                    chat_history.append(_conversation_message("assistant", error_text, trace["assistant_message_id"]))
                    _persist_turn_trace(trace, username, session_id, region, error=e)
                    yield (chat_history, "", *_hidden_interrupt_form_updates())
                    return
    
                _persist_turn_trace(trace, username, session_id, region, result_holder.get("result"))
                try:
                    chat_store.log_chat_turn(username, session_id, region, chat_history)
                except Exception:
                    logger.exception("Failed to persist chat history for session %s", session_id)
    
                yield (chat_history, "", *_interrupt_form_updates(result_holder.get("result")))
    
            async def submit_interrupt_answers(
                interrupt_meta, chat_history, verbose, username, session_id, region, *field_values
            ):
                chat_history = list(chat_history or [])
                if not interrupt_meta:
                    yield (chat_history, *_hidden_interrupt_form_updates())
                    return
    
                n = MAX_INTERRUPT_FIELDS
                text_values = field_values[:n]
                dropdown_values = field_values[n:2 * n]
                fields = interrupt_meta.get("fields") or []
                answers = {}
                display_lines = []
                for i, field in enumerate(fields):
                    name = field.get("name") or f"field_{i}"
                    value = dropdown_values[i] if _interrupt_field_type(field) == "choice" else text_values[i]
                    answers[name] = value
                    display_lines.append(f"- **{field.get('label') or name}**: {value}")
    
                region = (region or "apac").strip().lower()
                chat_store.set_session_context(username, session_id, region)
                user_message = "\n".join(display_lines) or "(submitted)"
                trace = _new_turn_trace(user_message)
                chat_history.append(
                    _conversation_message("user", user_message, trace["user_message_id"])
                )
                invocation_index = len(chat_history)
                chat_history.append(_activity_message("Agent", "Resuming...", message_id="agent-status"))
                # Hide the form immediately -- don't wait for the whole agent turn to finish.
                yield (chat_history, *_hidden_interrupt_form_updates())
    
                global agent
                agent = _ensure_chat_agent(region)
                responses = [{"interruptResponse": {"interruptId": interrupt_meta["interrupt_id"], "response": answers}}]
                result_holder: dict = {}
                try:
                    async for updated_history in _drive_agent_stream(
                        responses, chat_history, invocation_index, verbose, result_holder, trace
                    ):
                        yield (updated_history, *([gr.skip()] * _INTERRUPT_FORM_OUTPUT_COUNT))
                except Exception as e:
                    logger.exception("Agent error resuming interrupt")
                    chat_history[invocation_index]["content"] = "Failed."
                    chat_history[invocation_index]["metadata"]["status"] = "done"
                    error_text = f"Error: {e}"
                    trace["assistant_response"] = error_text
                    chat_history.append(_conversation_message("assistant", error_text, trace["assistant_message_id"]))
                    _persist_turn_trace(trace, username, session_id, region, error=e)
                    yield (chat_history, *_hidden_interrupt_form_updates())
                    return
    
                _persist_turn_trace(trace, username, session_id, region, result_holder.get("result"))
                try:
                    chat_store.log_chat_turn(username, session_id, region, chat_history)
                except Exception:
                    logger.exception("Failed to persist chat history for session %s", session_id)
    
                yield (chat_history, *_interrupt_form_updates(result_holder.get("result")))
    
            msg.submit(None, None, None, js=SCROLL_TO_CHAT_JS, queue=False)
            msg.submit(lambda: gr.update(visible=False), None, starter_prompts_row, queue=False)
            msg.submit(
                respond_async,
                [msg, chatbot, auth_state, verbose_mode, username_state, session_id_state, region_state],
                [chatbot, msg, *interrupt_form_outputs],
            ).then(
                refresh_pending_changes,
                [username_state, session_id_state, region_state, pending_label_to_id],
                [pending_checkboxes, pending_label_to_id, pending_accordion],
            )
            send_button.click(None, None, None, js=SCROLL_TO_CHAT_JS, queue=False)
            send_button.click(lambda: gr.update(visible=False), None, starter_prompts_row, queue=False)
            send_button.click(
                respond_async,
                [msg, chatbot, auth_state, verbose_mode, username_state, session_id_state, region_state],
                [chatbot, msg, *interrupt_form_outputs],
            ).then(
                refresh_pending_changes,
                [username_state, session_id_state, region_state, pending_label_to_id],
                [pending_checkboxes, pending_label_to_id, pending_accordion],
            )

            # Starter prompts: fill the message box and submit in one click, no typing needed.
            STARTER_PROMPTS = {
                starter_btn_1: "I want to create a new DQ dataset from Redshift or S3.",
                starter_btn_2: "Help me inspect an existing dataset's definition, rules, and alerts.",
                starter_btn_3: "I want to modify an existing dataset's configuration (metaTags, schedule, profile settings, alerts, custom rules and layer rules etc).",
                starter_btn_4: "Help me investigate DQ findings, adaptive rule breaks, or delta profiles for a dataset.",
                starter_btn_5: "I want to submit a Jira DQ change request or new dataset set-up ticket.",
            }
            for starter_btn, starter_text in STARTER_PROMPTS.items():
                starter_btn.click(None, None, None, js=SCROLL_TO_CHAT_JS, queue=False)
                starter_btn.click(
                    lambda text=starter_text: (text, gr.update(visible=False)),
                    None,
                    [msg, starter_prompts_row],
                    queue=False,
                ).then(
                    respond_async,
                    [msg, chatbot, auth_state, verbose_mode, username_state, session_id_state, region_state],
                    [chatbot, msg, *interrupt_form_outputs],
                ).then(
                    refresh_pending_changes,
                    [username_state, session_id_state, region_state, pending_label_to_id],
                    [pending_checkboxes, pending_label_to_id, pending_accordion],
                )
    
            interrupt_submit_btn.click(None, None, None, js=SCROLL_TO_CHAT_JS, queue=False)
            interrupt_submit_btn.click(
                submit_interrupt_answers,
                [
                    interrupt_state, chatbot, verbose_mode, username_state, session_id_state, region_state,
                    *interrupt_field_inputs, *interrupt_field_dropdowns,
                ],
                [chatbot, *interrupt_form_outputs],
            ).then(
                refresh_pending_changes,
                [username_state, session_id_state, region_state, pending_label_to_id],
                [pending_checkboxes, pending_label_to_id, pending_accordion],
            )
    
            pending_refresh_btn.click(
                refresh_pending_changes,
                [username_state, session_id_state, region_state, pending_label_to_id],
                [pending_checkboxes, pending_label_to_id, pending_accordion],
            )
            pending_apply_btn.click(None, None, None, js=SCROLL_TO_CHAT_JS, queue=False)
            pending_apply_btn.click(
                apply_selected_pending_changes,
                [
                    pending_checkboxes, pending_label_to_id, pending_reason, chatbot, verbose_mode,
                    username_state, session_id_state, region_state,
                ],
                [pending_status, pending_checkboxes, pending_label_to_id, pending_accordion, chatbot, *interrupt_form_outputs],
            )
            
            # Prompt submit handler: render template and populate message field
            def on_prompt_submit_click(template_obj, field_types, prompt_title_str, *all_field_values):
                """
                Construct the final prompt from template and user inputs.
                Returns the rendered prompt text to be placed in the message field.
                """
                if not template_obj:
                    # No template: the user typed their own prompt into the Prompt box.
                    if prompt_title_str and prompt_title_str.strip():
                        return gr.update(value=prompt_title_str.strip())
                    return gr.update(value="Error: No prompt selected")
                
                # Split field values: first 8 are inputs, next 8 are dropdowns
                field_inputs = all_field_values[:8]
                field_dropdowns = all_field_values[8:16]
                
                # Map field values to field names
                field_dict = {}
                fields = template_obj.get_all_fields()
                
                for i, field in enumerate(fields):
                    # Use dropdown value if field is dropdown type, else use textbox value
                    field_type = field_types[i] if i < len(field_types) else 'text'
                    if field_type == 'dropdown':
                        value = field_dropdowns[i] if i < len(field_dropdowns) else None
                    else:
                        value = field_inputs[i] if i < len(field_inputs) else None
                    
                    if value:
                        field_dict[field["name"]] = value
                
                # Render template
                final_prompt, missing = template_obj.render(field_dict)
                
                if missing:
                    error_msg = f"⚠️  Missing required fields: {', '.join(missing)}\n\nPlease fill in all fields marked with *."
                    return gr.update(value=error_msg)
                
                return gr.update(value=final_prompt)
            
            # Validation function to enable/disable button based on required fields
            def validate_and_enable_button(template, field_types, prompt_title_str, *all_field_values):
                """Check if all required fields are filled; enable button only if they are."""
                if not template:
                    # Free-text prompt is valid on its own.
                    return gr.update(interactive=bool(prompt_title_str and prompt_title_str.strip()))
                
                field_inputs = all_field_values[:8]
                field_dropdowns = all_field_values[8:16]
                field_types = field_types or []
                
                fields = template.get_all_fields()
                
                # Check each required field
                for i, field in enumerate(fields):
                    if field.get("required"):
                        field_type = field_types[i] if i < len(field_types) else 'text'
                        if field_type == 'dropdown':
                            value = field_dropdowns[i] if i < len(field_dropdowns) else None
                        else:
                            value = field_inputs[i] if i < len(field_inputs) else None
                        
                        # If required field is empty, disable button
                        if not value or (isinstance(value, str) and not value.strip()):
                            return gr.update(interactive=False)
                
                # All required fields filled, enable button
                return gr.update(interactive=True)
            
            # Add change event handlers to all field inputs to validate
            validation_inputs = [current_template_state, field_types_state, prompt_title] + prompt_field_inputs + prompt_field_dropdowns
            for field_component in prompt_field_inputs + prompt_field_dropdowns:
                field_component.change(
                    validate_and_enable_button,
                    validation_inputs,
                    [prompt_submit],
                    queue=False
                )
            
            prompt_submit.click(
                on_prompt_submit_click,
                [current_template_state, field_types_state, prompt_title] + prompt_field_inputs + prompt_field_dropdowns,
                [msg],
                queue=False
            ).then(
                lambda: (
                    None, gr.update(visible=False), None, [], gr.update(interactive=False),
                    *([gr.update(visible=False) for _ in range(8)] +  # field_rows
                      [gr.update(visible=False, value="") for _ in range(8)] +  # field_inputs
                      [gr.update(visible=False, choices=[], value=None) for _ in range(8)] +  # field_dropdowns
                      [gr.update(value="Field") for _ in range(8)] +  # field_labels
                      [gr.update(visible=False) for _ in range(8)])  # field_query_btns
                ),
                None,
                [prompt_title, prompt_form_group, current_template_state, field_types_state, prompt_submit] + prompt_field_rows + prompt_field_inputs + prompt_field_dropdowns + prompt_field_labels + prompt_field_query_btns,
                queue=False
            ).then(
                None,
                None,
                None,
                js="""
                () => {
                    const box = document.querySelector('#msg_box_wrap');
                    if (box) {
                        box.scrollIntoView({ behavior: 'smooth', block: 'center' });
                        box.querySelector('textarea')?.focus();
                    }
                }
                """,
                queue=False
            )
            
            # Clear form button
            prompt_clear_form.click(
                lambda: (
                    None, gr.update(visible=False), None, [], gr.update(interactive=False),
                    *([gr.update(visible=False) for _ in range(8)] +  # field_rows
                      [gr.update(visible=False, value="") for _ in range(8)] +  # field_inputs
                      [gr.update(visible=False, choices=[], value=None) for _ in range(8)] +  # field_dropdowns
                      [gr.update(value="Field") for _ in range(8)] +  # field_labels
                      [gr.update(visible=False) for _ in range(8)])  # field_query_btns
                ),
                None,
                [prompt_title, prompt_form_group, current_template_state, field_types_state, prompt_submit] + prompt_field_rows + prompt_field_inputs + prompt_field_dropdowns + prompt_field_labels + prompt_field_query_btns,
                queue=False
            )
            
            # Initialize prompt dropdown when page loads
            def on_page_load():
                if default_category:
                    prompts = get_prompts_for_category(default_category)
                    choices_list = prompts if prompts else []
                    if len(choices_list) == 1:
                        return gr.update(choices=choices_list, value=choices_list[0])
                    return gr.update(choices=choices_list, value=None)
                return gr.update(choices=[], value=None)
            
            demo.load(on_page_load, None, [prompt_title])
            demo.load(None, None, None, js=ENTER_TO_SUBMIT_JS)
            
            clear.click(
                lambda: ([], gr.update(visible=False), None, "", ""),
                None,
                [chatbot, feedback_form_group, feedback_selection_state, feedback_comment, feedback_status],
                queue=False,
            )
    
            resume_yes_btn.click(
                resume_previous_chat,
                [resumable_history_state],
                [chatbot, session_id_state, resume_banner, resume_row, starter_prompts_row],
            )
            resume_no_btn.click(
                dismiss_resume_prompt,
                [region_state],
                [resume_banner, resume_row, chatbot, starter_prompts_row],
            )

    login_button.click(
        authenticate, 
        inputs=[region_input, username_input, password_input], 
        outputs=[login_output, auth_state, username_state, region_state]
    ).then(
        lambda: gr.Tabs(selected="chat_tab"),
        None,
        tabs,
    ).then(
        check_resume,
        inputs=[username_state, region_state],
        outputs=[resume_banner, resume_row, resumable_history_state, chatbot, starter_prompts_row],
    )

if __name__ == "__main__":
    # Containers set GRADIO_SERVER_NAME=0.0.0.0 so the ALB can reach the app.
    server_name = os.getenv("GRADIO_SERVER_NAME", "127.0.0.1")
    chat_store.ensure_tables()
    logger.info("Starting DQ Chatbot on %s:7860", server_name)
    demo.launch(server_name=server_name, server_port=7860, share=False, css=APP_CSS)

# lsof -nP -iTCP:7860 -sTCP:LISTEN
# kill 6154
# lsof -nP -iTCP:7860 -sTCP:LISTEN || echo "port 7860 is free"
