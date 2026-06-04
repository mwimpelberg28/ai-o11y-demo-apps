"""Anthropic (Claude) provider — real implementation.

Calls api.anthropic.com via the official `anthropic` Python SDK.
Emits a Sigil generation event per call (start_generation / set_result /
close) following the documented pattern from the Sigil agent-first
instrumentation brief.

Phase 1 scope: non-streaming `messages.create()` only. Supports tool use
in both directions (sees tool_use response blocks; accepts tool_result
input blocks). Streaming and prompt-caching land later.

Message translation pulled from observibelity's battle-tested helper —
Anthropic rejects OpenAI-style `role: "tool"` messages and expects
`role: "user"` with a `tool_result` content block instead.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import random
from typing import Any

from anthropic import AsyncAnthropic
from sigil_sdk import (
    Generation,
    GenerationStart,
    Message,
    MessageRole,
    ModelRef,
    ToolCall,
    ToolResult,
    TokenUsage,
    text_part,
    tool_call_part,
    tool_result_part,
)


def _first_user_text(messages: list[dict[str, Any]]) -> str:
    """First user message's plain text, truncated for Sigil conversation_title.

    Handles both string content (Ollama/OpenAI style) and list-of-blocks
    content (Anthropic native style, e.g. [{"type": "text", "text": "..."}]).
    """
    for m in messages:
        if m.get("role") != "user":
            continue
        c = m.get("content", "")
        if isinstance(c, str):
            return c[:80]
        if isinstance(c, list):
            for block in c:
                if isinstance(block, dict) and block.get("type") == "text":
                    return (block.get("text") or "")[:80]
    return ""


_ROLE_MAP = {
    "user": MessageRole.USER,
    "assistant": MessageRole.ASSISTANT,
    "tool": MessageRole.TOOL,
}


def _extract_text(content: Any) -> str:
    """Flatten a request message's `content` field to plain text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            b.get("text", "") for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        )
    return ""


def _to_sigil_input(messages: list[dict[str, Any]]) -> list[Message]:
    """Convert request messages → sigil_sdk Messages for Generation.input.

    Captures the user's prompt + prior turns so Sigil's Conversation Thread
    view shows the back-and-forth, not just the assistant's output. System
    messages are NOT included here — they go on GenerationStart.system_prompt
    instead (Sigil treats them as a separate first-class field).

    Tool messages get a `tool_result_part` (not a `text_part`) so Sigil
    renders them as a tool result block in the thread, with `is_error=true`
    surfacing the red "ERROR" badge whenever the specialist marked the
    role=tool message as a failure (e.g. mice trap).
    """
    out: list[Message] = []
    for m in messages:
        raw_role = m.get("role", "")
        role = _ROLE_MAP.get(raw_role)
        if role is None:
            continue
        text = _extract_text(m.get("content", ""))
        if raw_role == "tool":
            out.append(Message(role=role, parts=[tool_result_part(ToolResult(
                tool_call_id=m.get("tool_call_id") or m.get("id") or "",
                name=m.get("name") or "",
                content=text,
                content_json=b"",
                is_error=bool(m.get("is_error")),
            ))]))
        elif text:
            out.append(Message(role=role, parts=[text_part(text)]))
    return out


def _extract_system_prompt(messages: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for m in messages:
        if m.get("role") == "system":
            t = _extract_text(m.get("content", ""))
            if t:
                parts.append(t)
    return "\n\n".join(parts)

from ..metrics import record_cost, record_user_call, record_user_tokens
from ..pricing import calculate_cost
from .base import ProviderRequest, ProviderResponse

log = logging.getLogger(__name__)

PROVIDER_NAME = "anthropic"
DEFAULT_MODEL = os.getenv("ANTHROPIC_DEFAULT_MODEL", "claude-haiku-4-5-20251001")
# Hard cap on max_tokens to keep $/day predictable. Specialists send 1024 by
# default; this clamps the per-call ceiling. Tunable via env.
_MAX_TOKENS_CAP = int(os.getenv("ANTHROPIC_MAX_TOKENS_CAP", "1024"))


def _parse_model_weights(env_value: str) -> list[tuple[str, float]]:
    """Parse "model_a:weight_a,model_b:weight_b" env value.

    Weights normalized to sum to 1.0. Returns [] when unset/blank.
    """
    if not env_value or not env_value.strip():
        return []
    pairs: list[tuple[str, float]] = []
    for entry in env_value.split(","):
        entry = entry.strip()
        if not entry:
            continue
        if ":" not in entry:
            log.warning("ANTHROPIC_MODEL_WEIGHTS entry missing ':': %r", entry)
            continue
        model, w_str = entry.rsplit(":", 1)
        try:
            w = float(w_str)
        except ValueError:
            log.warning("ANTHROPIC_MODEL_WEIGHTS bad weight %r in %r", w_str, entry)
            continue
        if w <= 0:
            continue
        pairs.append((model.strip(), w))
    total = sum(w for _, w in pairs)
    if total <= 0:
        return []
    return [(m, w / total) for m, w in pairs]


_MODEL_WEIGHTS = _parse_model_weights(os.getenv("ANTHROPIC_MODEL_WEIGHTS", ""))
if _MODEL_WEIGHTS:
    log.info(
        "anthropic provider: weighted model pool: %s",
        ", ".join(f"{m}={w:.0%}" for m, w in _MODEL_WEIGHTS),
    )

# When > 0, each user_id gets a deterministic K-model "personal pool" sampled
# from the weight CDF at K evenly-spaced user-deterministic offsets. The call's
# conversation_id picks uniformly across the K lanes — which preserves the
# company-level weight distribution exactly (each lane is uniform over [0,1)
# in expectation, so P(model M) = w_M per lane). Duplicate lanes are allowed
# (skewed weights can put two lanes in the same model's CDF slice), which
# means a user's effective pool can be smaller than K.
_USER_POOL_SIZE = int(os.getenv("ANTHROPIC_USER_POOL_SIZE", "0"))
if _USER_POOL_SIZE > 0 and _MODEL_WEIGHTS:
    log.info("anthropic provider: per-user pool size K=%d", _USER_POOL_SIZE)


def _walk_cdf(position: float) -> str:
    """Return the model whose CDF slice contains `position` in [0, 1)."""
    cumulative = 0.0
    for model, w in _MODEL_WEIGHTS:
        cumulative += w
        if position < cumulative:
            return model
    return _MODEL_WEIGHTS[-1][0]


def _pick_model(req: ProviderRequest) -> str:
    """Pick a model. Priority: explicit req.model -> user-pool / weighted-sticky -> default.

    When ANTHROPIC_USER_POOL_SIZE > 0 and req.user_id is set, the user gets a
    K-model personal pool and conversations rotate uniformly among those K
    lanes. Otherwise we fall back to plain sticky-per-conversation weighted
    selection.
    """
    if req.model:
        return req.model
    if not _MODEL_WEIGHTS:
        return DEFAULT_MODEL

    # User-pool stratification: K offsets on the weight CDF picked from a
    # user-deterministic origin, lane chosen by conversation hash. Preserves
    # company-level distribution exactly while constraining each user to <= K
    # distinct models.
    if _USER_POOL_SIZE > 0 and req.user_id:
        user_hash = int(hashlib.md5(req.user_id.encode()).hexdigest()[:8], 16) / 0xFFFFFFFF
        conv_key = (req.session_id or "") + "|" + (req.conversation_id or "")
        if conv_key.strip("|"):
            lane = int(hashlib.md5(conv_key.encode()).hexdigest()[:8], 16) % _USER_POOL_SIZE
        else:
            lane = random.randrange(_USER_POOL_SIZE)
        position = (user_hash + lane / _USER_POOL_SIZE) % 1.0
        return _walk_cdf(position)

    # Sticky-per-conversation: deterministic (session_id, conversation_id) -> model.
    # The `|model` salt decorrelates this hash from the router's provider-pick
    # hash on the same key — without it, anthropic's PROVIDER_WEIGHTS slice
    # nests inside haiku's MODEL_WEIGHTS slice and Sonnet/Opus become
    # unreachable from synthetic traffic.
    sticky_key = (req.session_id or "") + "|" + (req.conversation_id or "") + "|model"
    if sticky_key.strip("|"):
        h = int(hashlib.md5(sticky_key.encode()).hexdigest()[:8], 16) / 0xFFFFFFFF
        return _walk_cdf(h)
    # No session info — plain weighted random
    return _walk_cdf(random.random())


def _to_anthropic_messages(messages: list[dict]) -> tuple[str | None, list[dict]]:
    """OpenAI-style messages -> (system, anthropic messages).

    Handles:
      - role=system -> pulled out into a separate `system` parameter
      - role=tool   -> rewritten as role=user with a tool_result content block
      - assistant with tool_calls -> expanded into tool_use content blocks
    """
    system_parts: list[str] = []
    anth_messages: list[dict] = []

    for msg in messages:
        role = msg.get("role")

        if role == "system":
            content = msg.get("content")
            if isinstance(content, str):
                system_parts.append(content)
            elif isinstance(content, list):
                system_parts.extend(p.get("text", "") for p in content if isinstance(p, dict))
            continue

        if role == "tool":
            tool_block: dict[str, Any] = {
                "type": "tool_result",
                "tool_use_id": msg.get("tool_call_id") or msg.get("id") or "toolu_unknown",
                "content": str(msg.get("content", "")),
            }
            # Anthropic API supports is_error=true on tool_result so the
            # model knows the tool failed and writes a recovery reply;
            # Sigil also picks this up on the input message and renders
            # an "ERROR" badge in the conversation thread.
            if msg.get("is_error"):
                tool_block["is_error"] = True
            anth_messages.append({
                "role": "user",
                "content": [tool_block],
            })
            continue

        if role == "assistant" and msg.get("tool_calls"):
            blocks: list[dict] = []
            if msg.get("content"):
                blocks.append({"type": "text", "text": str(msg["content"])})
            for tc in msg["tool_calls"]:
                fn = tc.get("function") or {}
                blocks.append({
                    "type": "tool_use",
                    "id": tc.get("id") or "toolu_unknown",
                    "name": tc.get("name") or fn.get("name") or "unknown",
                    "input": tc.get("input") or tc.get("args") or fn.get("arguments") or {},
                })
            anth_messages.append({"role": "assistant", "content": blocks})
            continue

        anth_messages.append({"role": role, "content": msg.get("content", "")})

    system = "\n\n".join(s for s in system_parts if s) or None
    return system, anth_messages


def _to_anthropic_tools(tools: list[dict] | None) -> list[dict] | None:
    """Normalize tool schemas to Anthropic shape.

    Accepts both our specialist tool schemas (already Anthropic-style with
    name/description/input_schema) AND OpenAI-style {type: function, function: ...}.
    """
    if not tools:
        return None
    out: list[dict] = []
    for t in tools:
        if "function" in t:
            fn = t["function"]
            out.append({
                "name": fn.get("name", "unknown"),
                "description": fn.get("description", ""),
                "input_schema": fn.get("parameters", {"type": "object", "properties": {}}),
            })
        else:
            out.append({
                "name": t.get("name"),
                "description": t.get("description", ""),
                "input_schema": t.get("input_schema", {"type": "object", "properties": {}}),
            })
    return out


def _parse_response(content_blocks: list[Any]) -> tuple[str, list[dict]]:
    """Extract text + tool_use blocks from Anthropic response content."""
    text_parts: list[str] = []
    tool_calls: list[dict] = []
    for block in content_blocks:
        btype = getattr(block, "type", None)
        if btype == "text":
            text_parts.append(getattr(block, "text", ""))
        elif btype == "tool_use":
            tool_calls.append({
                "id": getattr(block, "id", None),
                "name": getattr(block, "name", None),
                "input": getattr(block, "input", {}),
            })
    return "\n".join(text_parts), tool_calls


async def generate(req: ProviderRequest, sigil_client: Any) -> ProviderResponse:
    """Call Claude. Emit Sigil generation event. Return normalised response."""
    if not os.getenv("ANTHROPIC_API_KEY"):
        raise RuntimeError("ANTHROPIC_API_KEY not set")

    model = _pick_model(req)
    system, messages = _to_anthropic_messages(req.messages)
    tools = _to_anthropic_tools(req.tools)
    max_tokens = min(req.max_tokens or 1024, _MAX_TOKENS_CAP)

    kwargs: dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": messages,
    }
    if system is not None:
        kwargs["system"] = system
    if tools is not None:
        kwargs["tools"] = tools
    if req.temperature is not None:
        kwargs["temperature"] = req.temperature
    if req.top_p is not None:
        kwargs["top_p"] = req.top_p

    client = AsyncAnthropic()  # reads ANTHROPIC_API_KEY from env

    # Start a Sigil generation record — fire-and-forget if Sigil is misconfigured
    rec = None
    if sigil_client is not None:
        try:
            rec = sigil_client.start_generation(GenerationStart(
                # Always pass the RESOLVED model (via _pick_model) so error-path
                # generations carry a real `gen_ai.request.model` attribute.
                # Otherwise sigil-sdk drops empty-string attributes and the
                # error series renders as "unknown" in dashboards.
                model=ModelRef(provider=PROVIDER_NAME, name=model),
                agent_name=req.agent_name or "unknown-agent",
                agent_version=req.agent_version or "",
                conversation_id=req.conversation_id or "",
                conversation_title=_first_user_text(req.messages),
                system_prompt=_extract_system_prompt(req.messages),
                user_id=req.user_id or "",
                parent_generation_ids=req.parent_generation_ids or [],
                tags={
                    "app": req.app or "",
                    "caller_type": req.caller_type or "",
                    "session_id": req.session_id or "",
                    "user_id": req.user_id or "",
                    "gen_ai.system": PROVIDER_NAME,
                },
            ))
        except Exception:
            log.exception("sigil start_generation failed (continuing without Sigil)")
            rec = None

    async def _call_anthropic():
        """Make the Anthropic call; on 400 'temperature is deprecated', retry once without temperature."""
        try:
            return await client.messages.create(**kwargs)
        except Exception as exc:
            err_str = str(exc).lower()
            # Reasoning-tier models (Opus 4.7+ "thinking" variants) 400 when
            # temperature is provided. Strip it and retry once.
            if (
                "temperature" in err_str
                and ("deprecated" in err_str or "not supported" in err_str)
                and "temperature" in kwargs
            ):
                log.info("anthropic %s rejects temperature; retrying without", model)
                kwargs.pop("temperature", None)
                return await client.messages.create(**kwargs)
            raise

    try:
        response = await _call_anthropic()
    except Exception as e:
        # Best-effort error reporting to Sigil before re-raising
        if rec is not None:
            try:
                # Carry the request messages onto the errored generation so
                # Sigil's conversation thread renders the user's question even
                # when Anthropic 5xx'd. Without this, set_call_error alone
                # produces "No messages in this turn" in the UI.
                rec.set_result(generation=Generation(
                    model=ModelRef(provider=PROVIDER_NAME, name=model),
                    response_model="",
                    stop_reason="error",
                    input=_to_sigil_input(req.messages),
                    output=[],
                    usage=TokenUsage(input_tokens=0, output_tokens=0),
                ))
                rec.set_call_error(error=e)
                rec.end()
            except Exception:
                log.exception("sigil error reporting failed")
        raise

    content_text, tool_calls = _parse_response(response.content)
    usage = response.usage
    input_tokens = getattr(usage, "input_tokens", 0) or 0
    output_tokens = getattr(usage, "output_tokens", 0) or 0
    cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
    cache_create = getattr(usage, "cache_creation_input_tokens", 0) or 0

    cost_usd = calculate_cost(PROVIDER_NAME, response.model, input_tokens, output_tokens)
    _attrs = dict(
        provider=PROVIDER_NAME,
        model=response.model or model,
        agent_name=req.agent_name or "unknown-agent",
        user_id=req.user_id or "",
    )
    record_cost(**_attrs, session_id=req.session_id or "", cost_usd=cost_usd)
    record_user_call(**_attrs)
    record_user_tokens(**_attrs, session_id=req.session_id or "",
                       conversation_id=req.conversation_id or "",
                       input_tokens=input_tokens, output_tokens=output_tokens)

    # Build the assistant Message that goes into Generation.output so Sigil
    # can count tool-call parts (gen_ai_client_tool_calls_per_operation) and
    # tag the gen_ai_tool_name label. Empty parts -> zero tool calls, which
    # is the correct outcome for text-only responses.
    output_parts = []
    if content_text:
        output_parts.append(text_part(content_text))
    for tc in tool_calls:
        try:
            input_json = json.dumps(tc.get("input") or {}).encode()
        except (TypeError, ValueError):
            input_json = b"{}"
        output_parts.append(tool_call_part(ToolCall(
            name=tc.get("name") or "unknown",
            id=tc.get("id") or "",
            input_json=input_json,
        )))
    output_messages = (
        [Message(role=MessageRole.ASSISTANT, parts=output_parts)]
        if output_parts
        else []
    )

    if rec is not None:
        try:
            rec.set_result(generation=Generation(
                model=ModelRef(provider=PROVIDER_NAME, name=response.model or model),
                response_id=response.id or "",
                response_model=response.model or "",
                stop_reason=response.stop_reason or "",
                input=_to_sigil_input(req.messages),
                output=output_messages,
                usage=TokenUsage(
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    cache_read_input_tokens=cache_read,
                    cache_write_input_tokens=cache_create,
                ),
            ))
            rec.end()
            if rec.err():
                log.warning("sigil generation rec.err(): %s", rec.err())
        except Exception:
            log.exception("sigil set_result/end failed")

    return ProviderResponse(
        content=content_text,
        model=response.model,
        provider=PROVIDER_NAME,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_input_tokens=cache_read,
        cache_creation_input_tokens=cache_create,
        cost_usd=cost_usd,
        finish_reason=response.stop_reason,
        response_id=response.id,
        generation_id=(getattr(rec, "generation_id", None) or getattr(rec, "id", None)) if rec is not None else None,
        tool_calls=tool_calls,
    )
