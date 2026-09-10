"""
Strands Agent factory for the local Collibra DQ chatbot.

Mirrors the Agent + JNJClaudeGatewayModel pattern in ../strands_agent.py, but
swaps the Atlassian MCP tools for the local `collibra_tools.py` function tools
(list/inspect datasets, resolve business units, suggest metaTags, and
propose/apply DatasetDef changes with a mandatory confirmation step).

Usage:
    from dq_agent import build_agent

    agent = build_agent()
    result = agent("What business unit is ds_conn_s3_angen_maf_target_ki_au mapped to?")
"""

from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterator, Optional, Sequence

import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from jnj_strands_model import JNJClaudeGatewayModel
from strands_agent import get_atlassian_mcp_client

from collibra_tools import ALL_TOOLS

if TYPE_CHECKING:
    from strands import Agent as AgentType

try:
    from strands import Agent
except Exception as exc:  # pragma: no cover - optional dependency guard
    Agent = None  # type: ignore[assignment,misc]
    _IMPORT_ERROR: Optional[Exception] = exc
else:
    _IMPORT_ERROR = None

logger = logging.getLogger(__name__)

_INSTRUCTION_FILE = Path(__file__).parent / "agent_instruction.txt"

FALLBACK_SYSTEM_PROMPT = (
    "You are a DQ automation assistant for J&J's Collibra DQ platform, used by "
    "super-users to inspect and modify dataset definitions. You have tools to "
    "list datasets, fetch DatasetDefs, resolve business units, suggest metaTags, "
    "and pull DQ findings/rules -- use these freely to answer questions. For any "
    "write action (propose_dataset_update, propose_dataset_create, "
    "propose_email_alert), first call the matching propose_* tool, show the "
    "resulting diff to the user in plain language, and ONLY call "
    "apply_dataset_change after the user explicitly confirms in the chat. Never "
    "call apply_dataset_change on your own initiative. Be concise and always "
    "state which dataset and region you acted on."
)


def _load_system_prompt() -> str:
    try:
        return _INSTRUCTION_FILE.read_text().strip() or FALLBACK_SYSTEM_PROMPT
    except FileNotFoundError:
        return FALLBACK_SYSTEM_PROMPT


def _require_strands() -> None:
    if Agent is None:
        raise ImportError(
            "The 'strands-agents' package is required. Install it (e.g. "
            "`pip install strands-agents`) to use dq_agent.py."
        ) from _IMPORT_ERROR


def get_model(**overrides: Any) -> JNJClaudeGatewayModel:
    """Build the J&J GenAI Gateway model provider used by the agent."""
    return JNJClaudeGatewayModel(**overrides)


def build_agent(
    system_prompt: Optional[str] = None,
    model: Optional[Any] = None,
    mcp_clients: Optional[Sequence[Any]] = None,
    **agent_kwargs: Any,
) -> "AgentType":
    """Build an Agent with local Collibra tools and optional MCP tool providers.

    MCPClient objects are passed directly so Strands can discover every paginated
    tool and manage the provider lifecycle. The caller remains responsible for
    calling ``agent.cleanup()``; use ``combined_agent_session`` when this factory
    should also create the Atlassian client.
    """
    _require_strands()
    tools = [*ALL_TOOLS, *(mcp_clients or [])]
    return Agent(
        model=model or get_model(),
        tools=tools,
        system_prompt=system_prompt or _load_system_prompt(),
        **agent_kwargs,
    )


@contextmanager
def combined_agent_session(
    system_prompt: Optional[str] = None,
    model: Optional[Any] = None,
    atlassian_tool_filters: Optional[Any] = None,
    **agent_kwargs: Any,
) -> Iterator["AgentType"]:
    """Yield an Agent combining local Collibra tools with Atlassian MCP tools."""
    mcp_client = get_atlassian_mcp_client(tool_filters=atlassian_tool_filters)
    agent = build_agent(
        system_prompt=system_prompt,
        model=model,
        mcp_clients=[mcp_client],
        **agent_kwargs,
    )
    try:
        yield agent
    finally:
        agent.cleanup()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
    agent = build_agent()
    print(f"Registered tools ({len(agent.tool_names)}): {sorted(agent.tool_names)}")
    print(agent("List the datasets you can see in APAC that contain 'sales'."))
