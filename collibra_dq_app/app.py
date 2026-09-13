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
import uuid

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

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

agent = None  # lazily built after successful login
mcp_client = None
mcp_status = "Atlassian MCP has not been initialized."
prompt_manager = PromptTemplateManager()

BU_MAPPING_TABLE = os.getenv("DQM_BU_MAPPING_TABLE", "public.dqm_business_unit_mapping")


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


def _display_chunks(text: str, size: int = 6):
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
        
        # --- Prompt Template Selector Section ---
        gr.Markdown("### Quick Prompts")
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
        
        def on_query_datasets():
            try:
                names = fetch_dataset_names()
            except Exception as exc:
                logger.exception("Failed to load dataset names")
                raise gr.Error(f"Could not load dataset names: {exc}") from exc
            if not names:
                gr.Warning("No datasets found in the business unit mapping table.")
            return gr.update(choices=names)
        
        for field_query_btn, field_dropdown in zip(prompt_field_query_btns, prompt_field_dropdowns):
            field_query_btn.click(on_query_datasets, None, [field_dropdown])
        
        # --- End Prompt Template Selector Section ---
        
        # gradio 6.x dropped the `type=` kwarg -- messages format is now the only format.
        chatbot = gr.Chatbot(height=600)
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
                            await asyncio.sleep(0.03)

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
        send_button.click(
            respond_async,
            [msg, chatbot, auth_state, verbose_mode, username_state, session_id_state],
            [chatbot, msg],
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
    demo.launch(server_name="127.0.0.1", server_port=7860, share=False, css=APP_CSS)

# lsof -nP -iTCP:7860 -sTCP:LISTEN
# kill 6154
# lsof -nP -iTCP:7860 -sTCP:LISTEN || echo "port 7860 is free"
