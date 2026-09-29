import asyncio
import json
import logging
import os
import re
import time
import uuid
from typing import Any, AsyncGenerator, Callable, Optional

import requests
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class _ToolsUnsupportedError(RuntimeError):
    """Raised when the gateway rejects a native ``tools`` payload."""


# The J&J gateway sometimes prepends one of these canned refusal-looking phrases to an
# otherwise normal, complete answer (observed even mid-conversation, not just on cold
# start) -- e.g. "Sorry, the model cannot answer this question." immediately followed by
# real content and tool calls. _strip_canned_prefix() removes just that leading noise so
# it isn't shown to the user; _post() only retries when NOTHING is left after stripping
# (i.e. a genuine empty/blocked response), to avoid paying for a wasted extra round trip
# on the common case where the real answer follows anyway.
_REFUSAL_PHRASES = (
    "sorry, the model cannot answer this question",
    "sorry, the model cannot answer",
    "i cannot answer",
    "i'm unable to answer",
    "i am unable to answer",
    "i'm not able to answer",
    "i cannot provide",
    "i'm not able to help with",
)

_REFUSAL_PREFIX_RE = re.compile(
    r"^\s*(?:" + "|".join(re.escape(p) for p in _REFUSAL_PHRASES) + r")[.!]*\s*",
    re.IGNORECASE,
)

# The refusal is the gateway's input guardrail scoring the payload, so an identical retry
# only helps for borderline scores -- keep it short rather than waiting minutes.
_REFUSAL_BACKOFF_SECONDS = (2, 4)


def _looks_like_canned_refusal(text: str) -> bool:
    lowered = (text or "").strip().lower()
    return any(phrase in lowered for phrase in _REFUSAL_PHRASES)


def _is_pure_canned_refusal(text: str) -> bool:
    """True if `text` is the canned refusal phrase and nothing else (used by _post's
    retry decision -- unlike _strip_canned_prefix, this does not fall back to the
    original text, since we need to detect the "nothing else was said" case here)."""
    if not text or not _looks_like_canned_refusal(text):
        return False
    return not _REFUSAL_PREFIX_RE.sub("", text, count=1).strip()


def _strip_canned_prefix(text: str) -> str:
    """Remove a known canned refusal-looking preamble from the start of `text`, if
    present. Leaves everything else untouched -- only strips a leading match.

    If nothing is left after stripping (the phrase WAS the entire answer), returns the
    original text unchanged instead of "" -- an empty string here previously caused
    _response_to_stream_events/_emit_text_events to silently emit zero content blocks,
    which the agent loop and UI both showed as no response at all rather than a visible
    (if unhelpful) reply.
    """
    if not text:
        return text
    stripped = _REFUSAL_PREFIX_RE.sub("", text, count=1)
    return stripped if stripped.strip() else text

try:
    from strands.models import Model
except Exception:
    class Model:  # type: ignore[no-redef]
        """Minimal fallback base when the optional 'strands' package is unavailable."""

        def get_config(self) -> dict[str, Any]:
            return {}

        def update_config(self, **kwargs: Any) -> None:
            _ = kwargs

        async def stream(self, *args: Any, **kwargs: Any) -> AsyncGenerator[dict[str, Any], None]:
            _ = args
            _ = kwargs
            if False:
                yield {}

try:
    from databricks.sdk import WorkspaceClient

    dbutils = WorkspaceClient().dbutils
except Exception:
    dbutils = None


SECRET_SCOPE = os.getenv("DATABRICKS_SECRET_SCOPE", "collibra")


def _load_secret_or_default(key: str, default: Any = None) -> Any:
    if dbutils is None:
        return default
    try:
        return dbutils.secrets.get(scope=SECRET_SCOPE, key=key)
    except Exception:
        return default


class JNJClaudeGatewayModel(Model):
    """
    Custom Strands model provider for J&J GenAI Gateway.

    Tested against the Strands 1.47.0 Model.stream signature:

        async def stream(
            self,
            messages,
            tool_specs=None,
            system_prompt=None,
            *,
            tool_choice=None,
            system_prompt_content=None,
            invocation_state=None,
            **kwargs
        ) -> AsyncIterable[StreamEvent]

    This class supports:
      - Agent(model=...)
      - agent("normal prompt")
      - agent(..., structured_output_model=YourPydanticModel)
      - model.structured_output(YourPydanticModel, prompt)

    Important:
      - Normal text generation is supported.
      - Structured output is supported via two paths:
          1. direct model.structured_output(...)
          2. Strands dynamic structured-output tool flow
      - General arbitrary external tool calling is not implemented here.
        This provider only handles the structured-output tool pattern.
    """

    def __init__(
        self,
        api_key: str | None = None,
        model_id: str = "global.anthropic.claude-sonnet-4-6",
        base_url: str = "https://genaiapigwna.jnj.com",
        max_tokens: int = 4096,
        temperature: float = 0.5,
        timeout: int = 120,
        anthropic_version: str = "bedrock-2023-05-31",
    ):
        self.api_key = api_key or _load_secret_or_default(
            "JNJ_GENAI_API_KEY", os.getenv("JNJ_GENAI_API_KEY")
        )
        if not self.api_key:
            raise ValueError(
                "Missing API key. Set api_key=..., JNJ_GENAI_API_KEY env var, "
                "or Databricks secret key JNJ_GENAI_API_KEY."
            )

        self.model_id = model_id
        self.base_url = base_url.rstrip("/")
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.timeout = timeout
        self.anthropic_version = anthropic_version
        # None = untried; False once the gateway has rejected a tools payload.
        self._tools_supported: bool | None = None

    # ---------------------------------------------------------------------
    # Required by strands.models.Model
    # ---------------------------------------------------------------------
    def get_config(self) -> dict[str, Any]:
        return {
            "model_id": self.model_id,
            "base_url": self.base_url,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
            "timeout": self.timeout,
            "anthropic_version": self.anthropic_version,
        }

    def update_config(self, **kwargs: Any) -> None:
        allowed = {
            "model_id",
            "base_url",
            "max_tokens",
            "temperature",
            "timeout",
            "anthropic_version",
        }

        unknown = set(kwargs) - allowed
        if unknown:
            raise ValueError(f"Unknown config option(s): {sorted(unknown)}")

        for key, value in kwargs.items():
            setattr(self, key, value)

        if "base_url" in kwargs:
            self.base_url = self.base_url.rstrip("/")

    async def stream(
        self,
        messages,
        tool_specs: list[dict[str, Any]] | None = None,
        system_prompt: str | None = None,
        *,
        tool_choice: dict[str, Any] | None = None,
        system_prompt_content: list[dict[str, Any]] | None = None,
        invocation_state: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> AsyncGenerator[dict[str, Any], None]:
        """
        Strands model streaming interface.

        For normal Agent(...) calls:
          yields Bedrock Converse-style text stream events.

        When tool_specs are supplied (MCP tools, structured-output tools, or
        both), the specs are forwarded to the gateway as native Anthropic
        ``tools`` so the model can genuinely call them. Any ``tool_use`` blocks
        returned by the model are emitted as toolUse stream events for the
        Strands event loop to execute.

        If the gateway rejects a tools payload, this falls back to the legacy
        prompt-based JSON path so structured output keeps working.
        """

        effective_system_prompt = self._merge_system_prompt(
            system_prompt=system_prompt,
            system_prompt_content=system_prompt_content,
        )

        if tool_specs and self._tools_supported is not False:
            task, retry_messages = self._start_retry_tracked_call(
                self._invoke_with_tools,
                messages,
                tool_specs,
                effective_system_prompt,
                tool_choice,
                kwargs,
            )
            async for progress_event in self._drain_retry_progress(task, retry_messages):
                yield progress_event
            try:
                response = task.result()
            except _ToolsUnsupportedError as exc:
                self._tools_supported = False
                logger.warning(
                    "J&J gateway rejected native tool calling; falling back to "
                    "prompt-based structured output. Detail: %s",
                    exc,
                )
            else:
                self._tools_supported = True
                for event in self._response_to_stream_events(response):
                    yield event
                return

        # Fallback: legacy structured-output-only handling via prompted JSON.
        if tool_specs:
            tool_spec = self._pick_structured_output_tool(tool_specs, tool_choice)

            if tool_spec is not None:
                tool_name, tool_schema = self._normalise_tool_spec(tool_spec)

                prompt = self._messages_to_plain_prompt(messages)
                result_obj = await asyncio.to_thread(
                    self._generate_json_object,
                    prompt,
                    tool_schema,
                    effective_system_prompt,
                    kwargs,
                )

                async for event in self._emit_tool_use_events(
                    tool_name=tool_name,
                    tool_input=result_obj,
                ):
                    yield event
                return

        # Normal text generation
        task, retry_messages = self._start_retry_tracked_call(
            self._invoke_text, messages, effective_system_prompt, kwargs
        )
        async for progress_event in self._drain_retry_progress(task, retry_messages):
            yield progress_event
        text = task.result()

        async for event in self._emit_text_events(text):
            yield event

    def _start_retry_tracked_call(self, func: Callable[..., Any], *args: Any) -> tuple["asyncio.Task[Any]", "asyncio.Queue[str]"]:
        """Kick off a blocking gateway call (`func`'s last param must accept an
        `on_retry` callback) in a worker thread, wiring that callback to a queue this
        event loop can drain concurrently -- see _drain_retry_progress. Needed because
        the whole call (incl. the canned-refusal backoff sleeps, up to ~1-2 minutes
        worst case) otherwise runs silently inside asyncio.to_thread with no visible
        progress until it fully completes."""
        loop = asyncio.get_running_loop()
        retry_messages: "asyncio.Queue[str]" = asyncio.Queue()

        def on_retry(attempt: int, max_attempts: int, delay: float) -> None:
            loop.call_soon_threadsafe(
                retry_messages.put_nowait,
                f"J&J gateway refused the request -- retrying ({attempt}/{max_attempts}, waiting {delay:.0f}s)...",
            )

        task = asyncio.ensure_future(asyncio.to_thread(func, *args, on_retry))
        return task, retry_messages

    async def _drain_retry_progress(
        self, task: "asyncio.Task[Any]", retry_messages: "asyncio.Queue[str]"
    ) -> AsyncGenerator[dict[str, Any], None]:
        """Yield a visible reasoningContent stream event for each retry message queued
        while `task` runs, so the UI shows progress instead of looking hung. Caller
        reads the actual result via `task.result()` once this generator is exhausted."""
        while not task.done():
            get_next = asyncio.ensure_future(retry_messages.get())
            done, _pending = await asyncio.wait({task, get_next}, return_when=asyncio.FIRST_COMPLETED)
            if get_next in done:
                yield self._retry_progress_event(get_next.result())
            else:
                get_next.cancel()
        while not retry_messages.empty():
            yield self._retry_progress_event(retry_messages.get_nowait())

    @staticmethod
    def _retry_progress_event(message: str) -> dict[str, Any]:
        return {
            "contentBlockDelta": {
                "contentBlockIndex": 0,
                "delta": {"reasoningContent": {"text": message + "\n"}},
            }
        }

    def structured_output(
        self,
        output_model: type[BaseModel],
        prompt: str,
        system_prompt: str | None = None,
        **kwargs: Any,
    ) -> BaseModel:
        """
        Direct structured output support.

        Example:
            result = model.structured_output(BookAnalysis, "Analyse this book...")
            print(result.title)

        This does not rely on Strands dynamic tools. It requests JSON directly
        and validates with Pydantic.
        """

        schema = output_model.model_json_schema()

        result_obj = self._generate_json_object(
            prompt=prompt,
            json_schema=schema,
            system_prompt=system_prompt,
            extra_payload=kwargs,
        )

        return self._validate_pydantic(output_model, result_obj)

    # ---------------------------------------------------------------------
    # J&J gateway invocation
    # ---------------------------------------------------------------------
    def _invoke_text(
        self,
        messages,
        system_prompt: str | None = None,
        extra_payload: dict[str, Any] | None = None,
        on_retry: Optional[Callable[[int, int, float], None]] = None,
    ) -> str:
        payload = self._build_payload(
            messages=messages,
            system_prompt=system_prompt,
            extra_payload=extra_payload,
        )

        result = self._post(payload, on_retry=on_retry)
        return _strip_canned_prefix(self._extract_text(result))

    def _invoke_with_tools(
        self,
        messages,
        tool_specs: list[dict[str, Any]],
        system_prompt: str | None,
        tool_choice: dict[str, Any] | None,
        extra_payload: dict[str, Any] | None = None,
        on_retry: Optional[Callable[[int, int, float], None]] = None,
    ) -> dict[str, Any]:
        """Invoke the gateway with native Anthropic tool calling enabled."""
        payload = self._build_payload(
            messages=messages,
            system_prompt=system_prompt,
            extra_payload=extra_payload,
            tool_specs=tool_specs,
            tool_choice=tool_choice,
        )

        try:
            return self._post(payload, on_retry=on_retry)
        except RuntimeError as exc:
            if self._looks_like_tools_unsupported(str(exc)):
                raise _ToolsUnsupportedError(str(exc)) from exc
            raise

    @staticmethod
    def _looks_like_tools_unsupported(error_text: str) -> bool:
        """Heuristically detect a gateway rejection of the tools payload."""
        lowered = error_text.lower()
        if "status=400" not in lowered:
            return False
        return any(
            token in lowered
            for token in ("tools", "tool_choice", "input_schema", "tool_use")
        )

    def _to_anthropic_tools(self, tool_specs: list[dict[str, Any]]) -> list[dict[str, Any]]:
        tools: list[dict[str, Any]] = []

        for spec in tool_specs:
            name, schema = self._normalise_tool_spec(spec)
            inner = spec.get("toolSpec", spec)
            tools.append(
                {
                    "name": name,
                    "description": inner.get("description") or name,
                    "input_schema": schema,
                }
            )

        return tools

    @staticmethod
    def _to_anthropic_tool_choice(tool_choice: dict[str, Any] | None) -> dict[str, Any] | None:
        if not isinstance(tool_choice, dict):
            return None

        chosen = tool_choice.get("tool")
        if isinstance(chosen, dict) and chosen.get("name"):
            return {"type": "tool", "name": chosen["name"]}

        if "any" in tool_choice:
            return {"type": "any"}
        if "auto" in tool_choice:
            return {"type": "auto"}
        if tool_choice.get("name"):
            return {"type": "tool", "name": tool_choice["name"]}

        return None

    def _generate_json_object(
        self,
        prompt: str,
        json_schema: dict[str, Any],
        system_prompt: str | None = None,
        extra_payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        json_instruction = f"""
            You must return ONLY valid JSON.

            Do not include markdown.
            Do not include comments.
            Do not include explanations.
            Do not wrap the JSON in a code block.

            The JSON must conform to this JSON Schema:

            {json.dumps(json_schema, ensure_ascii=False)}

            User task:

            {prompt}
            """.strip()

        messages = [
            {
                "role": "user",
                "content": json_instruction,
            }
        ]

        payload = self._build_payload(
            messages=messages,
            system_prompt=system_prompt,
            extra_payload=extra_payload,
        )

        # Encourage deterministic structured output.
        payload["temperature"] = extra_payload.get("temperature", 0) if extra_payload else 0

        result = self._post(payload)
        text = self._extract_text(result)
        return self._extract_json_object(text)

    def _build_payload(
        self,
        messages,
        system_prompt: str | None = None,
        extra_payload: dict[str, Any] | None = None,
        tool_specs: list[dict[str, Any]] | None = None,
        tool_choice: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload = {
            "anthropic_version": self.anthropic_version,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
            "messages": self._normalise_messages_for_anthropic(messages),
        }

        if system_prompt:
            payload["system"] = system_prompt

        if tool_specs:
            payload["tools"] = self._to_anthropic_tools(tool_specs)
            anthropic_tool_choice = self._to_anthropic_tool_choice(tool_choice)
            if anthropic_tool_choice:
                payload["tool_choice"] = anthropic_tool_choice

        # Allow only known generation controls from per-call kwargs.
        # Strands may pass internal fields (for example "model_state") that
        # are not accepted by the J&J gateway schema.
        allowed_runtime_overrides = {
            "anthropic_version",
            "max_tokens",
            "temperature",
            "top_p",
            "top_k",
            "stop_sequences",
            "metadata",
        }

        if extra_payload:
            for key, value in extra_payload.items():
                if value is not None and key in allowed_runtime_overrides:
                    payload[key] = value

        return payload

    def _post(
        self,
        payload: dict[str, Any],
        _attempt: int = 1,
        on_retry: Optional[Callable[[int, int, float], None]] = None,
    ) -> dict[str, Any]:
        url = f"{self.base_url}/model/{self.model_id}/invoke"

        headers = {
            "content-type": "application/json",
            "accept": "application/json",
            "x-api-key": self.api_key,
        }

        response = requests.post(
            url,
            json=payload,
            headers=headers,
            timeout=self.timeout,
        )

        try:
            response.raise_for_status()
        except requests.HTTPError as exc:
            raise RuntimeError(
                f"J&J GenAI Gateway request failed. "
                f"Status={response.status_code}, "
                f"PayloadKeys={sorted(payload.keys())}, "
                f"Body={response.text[:2000]}"
            ) from exc

        result = response.json()

        # Only worth retrying when the canned phrase is the WHOLE answer (nothing useful
        # left after stripping it) -- if real content follows, stripping it at display
        # time is enough and a further round trip would just waste time for nothing.
        # Observed in practice: this is a genuine gateway/model cold-start condition that
        # can take up to roughly a minute to clear (not a quick blip), so the backoff
        # below is intentionally long -- it only ever fires on the rare pure-refusal case,
        # never on a normal answer, so it doesn't slow down the common path.
        extracted = self._extract_text(result)
        if _attempt <= len(_REFUSAL_BACKOFF_SECONDS) and _is_pure_canned_refusal(extracted):
            delay = _REFUSAL_BACKOFF_SECONDS[_attempt - 1]
            max_attempts = len(_REFUSAL_BACKOFF_SECONDS) + 1
            logger.warning(
                "J&J gateway returned an empty canned refusal on attempt %d/%d "
                "(PayloadKeys=%s); retrying in %.0fs.",
                _attempt, max_attempts, sorted(payload.keys()), delay,
            )
            if on_retry:
                on_retry(_attempt, max_attempts, delay)
            time.sleep(delay)
            return self._post(payload, _attempt=_attempt + 1, on_retry=on_retry)

        return result

    # ---------------------------------------------------------------------
    # Stream event emitters expected by Strands event loop
    # ---------------------------------------------------------------------
    async def _emit_text_events(
        self,
        text: str,
    ) -> AsyncGenerator[dict[str, Any], None]:
        yield {
            "messageStart": {
                "role": "assistant",
            }
        }

        yield {
            "contentBlockDelta": {
                "contentBlockIndex": 0,
                "delta": {
                    "text": text,
                },
            }
        }

        yield {
            "contentBlockStop": {
                "contentBlockIndex": 0,
            }
        }

        yield {
            "messageStop": {
                "stopReason": "end_turn",
            }
        }

        yield {
            "metadata": {
                "usage": {
                    "inputTokens": 0,
                    "outputTokens": 0,
                    "totalTokens": 0,
                },
                "metrics": {
                    "latencyMs": 0,
                },
            }
        }

    async def _emit_tool_use_events(
        self,
        tool_name: str,
        tool_input: dict[str, Any],
    ) -> AsyncGenerator[dict[str, Any], None]:
        tool_use_id = f"tooluse_{uuid.uuid4().hex}"

        yield {
            "messageStart": {
                "role": "assistant",
            }
        }

        yield {
            "contentBlockStart": {
                "contentBlockIndex": 0,
                "start": {
                    "toolUse": {
                        "toolUseId": tool_use_id,
                        "name": tool_name,
                    }
                },
            }
        }

        yield {
            "contentBlockDelta": {
                "contentBlockIndex": 0,
                "delta": {
                    "toolUse": {
                        "input": json.dumps(tool_input, ensure_ascii=False),
                    }
                },
            }
        }

        yield {
            "contentBlockStop": {
                "contentBlockIndex": 0,
            }
        }

        yield {
            "messageStop": {
                "stopReason": "tool_use",
            }
        }

        yield {
            "metadata": {
                "usage": {
                    "inputTokens": 0,
                    "outputTokens": 0,
                    "totalTokens": 0,
                },
                "metrics": {
                    "latencyMs": 0,
                },
            }
        }

    # ---------------------------------------------------------------------
    # Message conversion
    # ---------------------------------------------------------------------
    _ANTHROPIC_STOP_REASONS = {
        "end_turn": "end_turn",
        "tool_use": "tool_use",
        "max_tokens": "max_tokens",
        "stop_sequence": "stop_sequence",
    }

    def _response_to_stream_events(self, response: dict[str, Any]):
        """Translate a native Anthropic response into Strands stream events.

        Emits a contentBlock per text / tool_use block so the Strands event loop
        can dispatch tool calls and continue the conversation.
        """
        content = response.get("content")
        if not isinstance(content, list):
            content = []

        yield {"messageStart": {"role": "assistant"}}

        block_index = 0
        saw_tool_use = False

        for block in content:
            if not isinstance(block, dict):
                continue

            block_type = block.get("type")

            if block_type == "text":
                text = _strip_canned_prefix(block.get("text") or "")
                if not text:
                    continue
                yield {
                    "contentBlockDelta": {
                        "contentBlockIndex": block_index,
                        "delta": {"text": text},
                    }
                }
                yield {"contentBlockStop": {"contentBlockIndex": block_index}}
                block_index += 1

            elif block_type == "thinking":
                thinking = block.get("thinking") or ""
                if thinking:
                    yield {
                        "contentBlockDelta": {
                            "contentBlockIndex": block_index,
                            "delta": {"reasoningContent": {"text": thinking}},
                        }
                    }
                signature = block.get("signature")
                if signature:
                    yield {
                        "contentBlockDelta": {
                            "contentBlockIndex": block_index,
                            "delta": {"reasoningContent": {"signature": signature}},
                        }
                    }
                yield {"contentBlockStop": {"contentBlockIndex": block_index}}
                block_index += 1

            elif block_type == "tool_use":
                saw_tool_use = True
                yield {
                    "contentBlockStart": {
                        "contentBlockIndex": block_index,
                        "start": {
                            "toolUse": {
                                "toolUseId": block.get("id") or f"tooluse_{uuid.uuid4().hex}",
                                "name": block.get("name"),
                            }
                        },
                    }
                }
                yield {
                    "contentBlockDelta": {
                        "contentBlockIndex": block_index,
                        "delta": {
                            "toolUse": {
                                "input": json.dumps(
                                    block.get("input") or {}, ensure_ascii=False
                                )
                            }
                        },
                    }
                }
                yield {"contentBlockStop": {"contentBlockIndex": block_index}}
                block_index += 1

        raw_stop = str(response.get("stop_reason") or "").strip()
        stop_reason = self._ANTHROPIC_STOP_REASONS.get(
            raw_stop, "tool_use" if saw_tool_use else "end_turn"
        )

        yield {"messageStop": {"stopReason": stop_reason}}
        yield {"metadata": self._extract_usage_metadata(response)}

    @staticmethod
    def _extract_usage_metadata(response: dict[str, Any]) -> dict[str, Any]:
        usage = response.get("usage") if isinstance(response.get("usage"), dict) else {}
        input_tokens = int(usage.get("input_tokens") or 0)
        output_tokens = int(usage.get("output_tokens") or 0)

        return {
            "usage": {
                "inputTokens": input_tokens,
                "outputTokens": output_tokens,
                "totalTokens": input_tokens + output_tokens,
            },
            "metrics": {"latencyMs": 0},
        }

    def _normalise_messages_for_anthropic(self, messages) -> list[dict[str, Any]]:
        """
        Converts Strands/Bedrock-style messages into native Anthropic content
        blocks, preserving tool_use / tool_result structure so multi-turn tool
        calling works.

        Handles:
          - {"role": "user", "content": "text"}
          - {"role": "user", "content": [{"text": "..."}]}
          - assistant toolUse blocks   -> Anthropic "tool_use"
          - user toolResult blocks     -> Anthropic "tool_result"
        """

        normalised: list[dict[str, Any]] = []

        for message in messages:
            role = message.get("role", "user")
            content = message.get("content", "")

            blocks: list[dict[str, Any]] = []

            if isinstance(content, str):
                if content:
                    blocks.append({"type": "text", "text": content})

            elif isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict):
                        blocks.append({"type": "text", "text": str(block)})
                        continue

                    if "text" in block:
                        if block["text"]:
                            blocks.append({"type": "text", "text": block["text"]})

                    elif "toolUse" in block:
                        tool_use = block["toolUse"] or {}
                        blocks.append(
                            {
                                "type": "tool_use",
                                "id": tool_use.get("toolUseId"),
                                "name": tool_use.get("name"),
                                "input": tool_use.get("input") or {},
                            }
                        )

                    elif "toolResult" in block:
                        blocks.append(self._to_anthropic_tool_result(block["toolResult"]))

                    else:
                        blocks.append(
                            {"type": "text", "text": json.dumps(block, ensure_ascii=False)}
                        )

            elif content:
                blocks.append({"type": "text", "text": str(content)})

            if not blocks:
                continue

            # Anthropic accepts only user/assistant roles.
            if role not in {"user", "assistant"}:
                role = "user"

            # Merge consecutive same-role messages; Anthropic requires alternation.
            if normalised and normalised[-1]["role"] == role:
                normalised[-1]["content"].extend(blocks)
            else:
                normalised.append({"role": role, "content": blocks})

        # Anthropic requires the conversation to start with a user message.
        if normalised and normalised[0]["role"] == "assistant":
            normalised.insert(
                0,
                {"role": "user", "content": [{"type": "text", "text": "Continue."}]},
            )

        return normalised

    @staticmethod
    def _to_anthropic_tool_result(tool_result: dict[str, Any] | None) -> dict[str, Any]:
        tool_result = tool_result or {}

        text_parts: list[str] = []
        for item in tool_result.get("content") or []:
            if not isinstance(item, dict):
                text_parts.append(str(item))
            elif "text" in item:
                text_parts.append(str(item["text"]))
            elif "json" in item:
                text_parts.append(json.dumps(item["json"], ensure_ascii=False))
            else:
                text_parts.append(json.dumps(item, ensure_ascii=False))

        return {
            "type": "tool_result",
            "tool_use_id": tool_result.get("toolUseId"),
            "content": [{"type": "text", "text": "\n".join(text_parts) or "(no output)"}],
            "is_error": str(tool_result.get("status") or "").lower() == "error",
        }

    def _messages_to_plain_prompt(self, messages) -> str:
        converted = self._normalise_messages_for_anthropic(messages)

        lines: list[str] = []
        for message in converted:
            parts: list[str] = []
            for block in message["content"]:
                if block.get("type") == "text":
                    parts.append(block.get("text", ""))
                else:
                    parts.append(json.dumps(block, ensure_ascii=False))
            lines.append(f"{message['role'].upper()}: " + "\n".join(parts))

        return "\n\n".join(lines)

    def _merge_system_prompt(
        self,
        system_prompt: str | None,
        system_prompt_content: list[dict[str, Any]] | None,
    ) -> str | None:
        parts = []

        # Strands passes the same prompt in both args; system_prompt_content is authoritative.
        if system_prompt and not system_prompt_content:
            parts.append(system_prompt)

        if system_prompt_content:
            for block in system_prompt_content:
                if isinstance(block, dict):
                    if "text" in block:
                        parts.append(block["text"])
                    else:
                        parts.append(json.dumps(block, ensure_ascii=False))
                else:
                    parts.append(str(block))

        merged = "\n\n".join(part for part in parts if part)
        return merged or None

    # ---------------------------------------------------------------------
    # Tool spec handling for Strands structured output
    # ---------------------------------------------------------------------
    def _pick_structured_output_tool(
        self,
        tool_specs: list[dict[str, Any]],
        tool_choice: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        """
        Strands structured output usually supplies a dynamic tool and forces
        tool_choice. This method picks that tool.

        If there is exactly one tool, we treat it as structured-output.
        If tool_choice explicitly names a tool, use that one.
        """

        if not tool_specs:
            return None

        chosen_name = None

        if isinstance(tool_choice, dict):
            if "tool" in tool_choice:
                tool_choice_tool = tool_choice["tool"]
                if isinstance(tool_choice_tool, dict):
                    chosen_name = tool_choice_tool.get("name")

            # Some versions/providers may use a flatter shape.
            chosen_name = chosen_name or tool_choice.get("name")

        if chosen_name:
            for spec in tool_specs:
                name, _schema = self._normalise_tool_spec(spec)
                if name == chosen_name:
                    return spec

        if len(tool_specs) == 1:
            return tool_specs[0]

        # Avoid pretending to support general multi-tool agent workflows here.
        return None

    def _normalise_tool_spec(
        self,
        tool_spec: dict[str, Any],
    ) -> tuple[str, dict[str, Any]]:
        """
        Supports the common Strands/Bedrock shape:

            {
              "name": "...",
              "description": "...",
              "inputSchema": {"json": {...}}
            }

        and:

            {
              "toolSpec": {
                "name": "...",
                "description": "...",
                "inputSchema": {"json": {...}}
              }
            }
        """

        spec = tool_spec.get("toolSpec", tool_spec)

        name = spec.get("name")
        if not name:
            raise ValueError(f"Cannot find tool name in tool spec: {tool_spec}")

        input_schema = spec.get("inputSchema", {})
        if isinstance(input_schema, dict) and "json" in input_schema:
            schema = input_schema["json"]
        else:
            schema = input_schema

        if not isinstance(schema, dict) or not schema:
            # Safe fallback if Strands passes a slightly different object.
            schema = {
                "type": "object",
                "additionalProperties": True,
            }

        return name, schema

    # ---------------------------------------------------------------------
    # Output parsing
    # ---------------------------------------------------------------------
    def _extract_text(self, result: dict[str, Any]) -> str:
        """
        Extracts text from your working response shape:

            {
              "content": [
                {"type": "text", "text": "..."}
              ]
            }

        Also includes fallback paths for common Bedrock/Anthropic variants.
        """

        if "content" in result and isinstance(result["content"], list):
            return "".join(
                block.get("text", "")
                for block in result["content"]
                if isinstance(block, dict) and block.get("type") == "text"
            )

        # Fallback: Bedrock Converse-like shape
        try:
            content = result["output"]["message"]["content"]
            return "".join(
                block.get("text", "")
                for block in content
                if isinstance(block, dict)
            )
        except Exception:
            pass

        # Last fallback
        return json.dumps(result, ensure_ascii=False)

    def _extract_json_object(self, text: str) -> dict[str, Any]:
        cleaned = text.strip()

        # Strip a leading canned refusal-looking preamble -- real JSON often still
        # follows it. Only a genuine refusal (nothing left after stripping) is fatal.
        cleaned = _strip_canned_prefix(cleaned).strip()
        if not cleaned:
            raise ValueError(f"Model refused to answer (content policy). Raw text:\n{text}")

        # Remove markdown code fence if the model ignored instructions.
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\s*```$", "", cleaned)

        try:
            parsed = json.loads(cleaned)
        except json.JSONDecodeError:
            # Extract the first JSON object from surrounding prose.
            match = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
            if not match:
                raise ValueError(f"Model did not return JSON. Raw text:\n{text}")

            parsed = json.loads(match.group(0))

        if not isinstance(parsed, dict):
            raise ValueError(f"Expected JSON object, got {type(parsed).__name__}: {parsed}")

        return parsed

    def _validate_pydantic(
        self,
        output_model: type[BaseModel],
        obj: dict[str, Any],
    ) -> BaseModel:
        # Pydantic v2
        if hasattr(output_model, "model_validate"):
            return output_model.model_validate(obj)

        # Pydantic v1 fallback
        return output_model.parse_obj(obj)
    

# '''------------ Usage: normal Agent(...) ------------'''
# import os
# from strands import Agent

# from jnj_strands_model import JNJClaudeGatewayModel


# model = JNJClaudeGatewayModel(api_key=os.getenv("JNJ_GENAI_API_KEY"),max_tokens=1024,temperature=0.5)

# agent = Agent(model=model,system_prompt="You are a helpful assistant.")

# result = agent("Explain LLMs in plain English in 5 bullet points.")

# print(result)


# '''------------ Usage: direct model.structured_output(...) 
# For Strands 1.x, the more current pattern is usually this: ------------'''

# from pydantic import BaseModel, Field
# from strands import Agent

# from jnj_strands_model import JNJClaudeGatewayModel


# class BookAnalysis(BaseModel):
#     title: str = Field(description="The book's title")
#     author: str = Field(description="The book's author")
#     genre: str = Field(description="Primary genre or category")
#     summary: str = Field(description="Brief summary of the book")
#     rating: int = Field(description="Rating from 1-10", ge=1, le=10)


# model = JNJClaudeGatewayModel(model_id="global.anthropic.claude-sonnet-4-6", api_key=os.getenv("JNJ_GENAI_API_KEY"))

# agent = Agent(
#     model=model,
#     system_prompt="You are a helpful assistant.",
# )

# result = agent(
#     """
#     Analyze this book: "The Hitchhiker's Guide to the Galaxy" by Douglas Adams.
#     It's a science fiction comedy about Arthur Dent's adventures through space
#     after Earth is destroyed. It's widely considered a classic of humorous sci-fi.
#     """,
#     structured_output_model=BookAnalysis,
# )

# book = result.structured_output

# print(book)
# print(book.title)
# print(book.author)
# print(book.genre)
# print(book.rating)
