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
import uuid

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

import gradio as gr

import chat_store
from dq_agent import build_agent
from strands_agent import get_atlassian_mcp_client

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

agent = None  # lazily built after successful login
mcp_client = None
mcp_status = "Atlassian MCP has not been initialized."


def _activity_message(title: str, content: str, status: str = "pending", message_id: str | None = None):
    metadata = {"title": title, "status": status}
    if message_id:
        metadata["id"] = message_id
    return {"role": "assistant", "content": content, "metadata": metadata}


def _tool_input_text(tool_input) -> str:
    if not tool_input:
        return "Waiting for tool input..."
    if isinstance(tool_input, str):
        text = tool_input
    else:
        text = json.dumps(tool_input, indent=2, default=str)
    return f"```json\n{text[:2000]}\n```"


def _display_chunks(text: str, size: int = 32):
    for start in range(0, len(text), size):
        yield text[start:start + size]


def _entry_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(block.get("text", "") for block in content if isinstance(block, dict))
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
        if metadata:  # activity/tool/reasoning rows all carry metadata; real turns don't
            continue
        text = _entry_text(entry.get("content"))
        if not text:
            continue
        messages.append({"role": role, "content": [{"text": text}]})
    return messages


def _build_chat_agent():
    """Build the persistent chat agent with local tools and Atlassian MCP tools."""
    global mcp_client, mcp_status
    try:
        mcp_client = get_atlassian_mcp_client()
        mcp_status = "Atlassian MCP tools are configured; Jira credentials will be validated on the first tool call."
        return build_agent(mcp_clients=[mcp_client])
    except Exception as exc:
        logger.warning("Atlassian MCP unavailable; starting with local Collibra tools only: %s", exc)
        mcp_status = f"Atlassian MCP unavailable; using local Collibra tools only ({exc})."
        return build_agent()


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


def authenticate(username: str, password: str):
    """Check username/password against the SUPERUSER_CREDENTIALS allowlist."""
    allowlist = _load_superuser_allowlist()
    if not allowlist:
        return "Login disabled: SUPERUSER_CREDENTIALS is not configured on the server.", False, ""
    if allowlist.get(username) == password and password:
        return f"Logged in as {username}.", True, username
    return "Invalid username or password.", False, ""


def check_resume(username: str):
    """After login, look for this user's most recent chat session and, if one exists,
    surface a prompt letting them resume it instead of starting fresh."""
    if not username:
        return gr.update(visible=False), gr.update(visible=False), None
    try:
        latest = chat_store.get_latest_session(username)
    except Exception:
        logger.exception("Failed to check for resumable chat history for user %s", username)
        latest = None
    if not latest or not latest.get("conversation_history"):
        # No history to offer -- make sure a stale agent from an earlier login in this
        # process doesn't leak unrelated context into a genuinely new session.
        if agent is not None:
            agent.messages = []
        return gr.update(visible=False), gr.update(visible=False), None
    message = (
        f"You have a previous conversation from {latest['updated_at']} "
        f"({latest['turn_count']} messages). Resume it?"
    )
    return gr.update(value=message, visible=True), gr.update(visible=True), latest


def resume_previous_chat(latest: dict | None):
    global agent
    if not latest:
        return [], uuid.uuid4().hex, gr.update(visible=False), gr.update(visible=False)
    if agent is None:
        agent = _build_chat_agent()
    agent.messages = _history_to_agent_messages(latest["conversation_history"])
    return latest["conversation_history"], latest["session_id"], gr.update(visible=False), gr.update(visible=False)


def dismiss_resume_prompt():
    if agent is not None:
        agent.messages = []
    return gr.update(visible=False), gr.update(visible=False)


with gr.Blocks(title="Collibra DQ Chatbot") as demo:
    gr.Markdown("# Collibra DQ Chatbot")
    gr.Markdown("Super-user access to inspect and modify Collibra DQ dataset definitions.")

    with gr.Tab("Login"):
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

    with gr.Tab("Chat"):
        resume_banner = gr.Markdown(visible=False)
        with gr.Row(visible=False) as resume_row:
            resume_yes_btn = gr.Button("Resume previous conversation")
            resume_no_btn = gr.Button("Start fresh")
        # gradio 6.x dropped the `type=` kwarg -- messages format is now the only format.
        chatbot = gr.Chatbot(height=600)
        msg = gr.Textbox(label="Message", placeholder="e.g. What business unit is ds_conn_s3_x mapped to?")
        verbose_mode = gr.Checkbox(label="Show activity and reasoning", value=True)
        clear = gr.Button("Clear")

        async def respond_async(message, chat_history, is_authed, verbose, username, session_id):
            chat_history = list(chat_history or [])
            if not is_authed:
                yield chat_history + [{"role": "assistant", "content": "Please log in on the Login tab first."}], ""
                return

            chat_store.set_session_context(username, session_id)
            chat_history.append({"role": "user", "content": message})
            invocation_index = len(chat_history)
            chat_history.append(_activity_message("Agent", "Preparing request...", message_id="agent-status"))
            yield chat_history, ""

            global agent
            if agent is None:
                agent = _build_chat_agent()
                chat_history[invocation_index]["content"] = mcp_status
                yield chat_history, ""

            try:
                result_text = ""
                answer_index = None
                reasoning_index = None
                reasoning_text = ""
                tool_indices: dict[str, int] = {}
                active_tool_ids: set[str] = set()

                def complete_active_tools():
                    for tool_id in active_tool_ids:
                        idx = tool_indices[tool_id]
                        chat_history[idx]["metadata"]["status"] = "done"
                    active_tool_ids.clear()

                async for event in agent.stream_async(message):
                    if event.get("init_event_loop"):
                        chat_history[invocation_index]["content"] = "Model initialized."
                        yield chat_history, ""

                    if event.get("start_event_loop"):
                        complete_active_tools()
                        chat_history[invocation_index]["content"] = "Evaluating next action..."
                        yield chat_history, ""

                    if event.get("reasoning") and event.get("reasoningText"):
                        reasoning_text += event["reasoningText"]
                        if verbose:
                            if reasoning_index is None:
                                reasoning_index = len(chat_history)
                                chat_history.append(
                                    _activity_message("Reasoning", reasoning_text, message_id="reasoning-trace")
                                )
                            else:
                                chat_history[reasoning_index]["content"] = reasoning_text
                            yield chat_history, ""

                    if "current_tool_use" in event:
                        tool_use = event["current_tool_use"]
                        tool_id = tool_use.get("toolUseId") or tool_use.get("name")
                        tool_name = tool_use.get("name") or "Unknown tool"
                        if tool_id:
                            if tool_id not in tool_indices:
                                tool_indices[tool_id] = len(chat_history)
                                chat_history.append(
                                    _activity_message(
                                        f"Running tool: {tool_name}",
                                        _tool_input_text(tool_use.get("input")),
                                        message_id=tool_id,
                                    )
                                )
                            else:
                                chat_history[tool_indices[tool_id]]["content"] = _tool_input_text(
                                    tool_use.get("input")
                                )
                            active_tool_ids.add(tool_id)
                            chat_history[invocation_index]["content"] = f"Waiting for {tool_name}..."
                            logger.info("Tool call: %s(%s)", tool_name, tool_use.get("input"))
                            yield chat_history, ""

                    if "data" in event:
                        complete_active_tools()
                        chat_history[invocation_index]["content"] = "Writing response..."
                        if answer_index is None:
                            answer_index = len(chat_history)
                            chat_history.append({"role": "assistant", "content": ""})
                        # The J&J gateway returns one complete text block, so chunk it for
                        # progressive display even though this is not true token streaming.
                        for chunk in _display_chunks(event["data"]):
                            result_text += chunk
                            chat_history[answer_index]["content"] = result_text
                            yield chat_history, ""
                            await asyncio.sleep(0.01)

                complete_active_tools()
                chat_history[invocation_index]["content"] = "Completed."
                chat_history[invocation_index]["metadata"]["status"] = "done"
                if reasoning_index is not None:
                    chat_history[reasoning_index]["metadata"]["status"] = "done"
                if answer_index is None:
                    chat_history.append({"role": "assistant", "content": result_text or "No response text returned."})

            except Exception as e:
                logger.exception("Agent error")
                chat_history[invocation_index]["content"] = "Failed."
                chat_history[invocation_index]["metadata"]["status"] = "done"
                chat_history.append({"role": "assistant", "content": f"Error: {e}"})

            try:
                chat_store.log_chat_turn(username, session_id, chat_history)
            except Exception:
                logger.exception("Failed to persist chat history for session %s", session_id)

            yield chat_history, ""

        msg.submit(
            respond_async,
            [msg, chatbot, auth_state, verbose_mode, username_state, session_id_state],
            [chatbot, msg],
        )
        clear.click(lambda: [], None, chatbot, queue=False)

        resume_yes_btn.click(
            resume_previous_chat,
            [resumable_history_state],
            [chatbot, session_id_state, resume_banner, resume_row],
        )
        resume_no_btn.click(dismiss_resume_prompt, None, [resume_banner, resume_row])

    login_button.click(
        authenticate, [username_input, password_input], [login_output, auth_state, username_state]
    ).then(
        check_resume, [username_state], [resume_banner, resume_row, resumable_history_state]
    )

if __name__ == "__main__":
    logger.info("Starting Collibra DQ Chatbot on port 7860 (localhost only)")
    demo.launch(server_name="127.0.0.1", server_port=7860, share=False)

# lsof -nP -iTCP:7860 -sTCP:LISTEN
# kill 6154
# lsof -nP -iTCP:7860 -sTCP:LISTEN || echo "port 7860 is free"
