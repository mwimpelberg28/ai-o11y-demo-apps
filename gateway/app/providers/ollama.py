"""Ollama provider — real implementation.

POSTs to {OLLAMA_BASE_URL}/api/chat using Ollama's native chat API.
Token counts come back as `prompt_eval_count` (input) and `eval_count`
(output). Tool-calling is supported by recent Ollama builds via the
`tools` request field.

Cost is computed via the static $/M-output-token rate from pricing.yaml
(see docs/METRICS.md — the GPU-power-derived formula is a deferred
extension).

Emits a Sigil generation event around every call. Same defensive pattern
as the Anthropic provider — if Sigil isn't configured or its API drifts,
the gateway logs a warning and keeps serving.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import random
from typing import Any

import httpx
from sigil_sdk import (
    Generation,
    GenerationStart,
    Message,
    MessageRole,
    ModelRef,
    ToolCall,
    TokenUsage,
    text_part,
    tool_call_part,
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
    view shows the back-and-forth, not just the assistant's output.
    """
    out: list[Message] = []
    for m in messages:
        role = _ROLE_MAP.get(m.get("role", ""))
        if role is None:
            continue
        text = _extract_text(m.get("content", ""))
        if text:
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

PROVIDER_NAME = "ollama"
DEFAULT_MODEL = os.getenv("OLLAMA_DEFAULT_MODEL", "qwen2.5:14b")


def _parse_model_weights(env_value: str) -> list[tuple[str, float]]:
    """`model_a:weight_a,model_b:weight_b` -> normalized [(model, weight)]. Empty list when unset."""
    if not env_value or not env_value.strip():
        return []
    pairs: list[tuple[str, float]] = []
    for entry in env_value.split(","):
        entry = entry.strip()
        if not entry or ":" not in entry:
            continue
        model, w_str = entry.rsplit(":", 1)
        try:
            w = float(w_str)
        except ValueError:
            continue
        if w > 0:
            pairs.append((model.strip(), w))
    total = sum(w for _, w in pairs)
    if total <= 0:
        return []
    return [(m, w / total) for m, w in pairs]


_MODEL_WEIGHTS = _parse_model_weights(os.getenv("OLLAMA_MODEL_WEIGHTS", ""))
if _MODEL_WEIGHTS:
    log.info(
        "ollama provider: weighted model pool: %s",
        ", ".join(f"{m}={w:.0%}" for m, w in _MODEL_WEIGHTS),
    )

# See anthropic.py for the full explanation of user-pool stratification.
_USER_POOL_SIZE = int(os.getenv("OLLAMA_USER_POOL_SIZE", "0"))
if _USER_POOL_SIZE > 0 and _MODEL_WEIGHTS:
    log.info("ollama provider: per-user pool size K=%d", _USER_POOL_SIZE)


def _walk_cdf(position: float) -> str:
    cumulative = 0.0
    for model, w in _MODEL_WEIGHTS:
        cumulative += w
        if position < cumulative:
            return model
    return _MODEL_WEIGHTS[-1][0]


def _pick_model(req: ProviderRequest) -> str:
    if req.model:
        return req.model
    if not _MODEL_WEIGHTS:
        return DEFAULT_MODEL

    if _USER_POOL_SIZE > 0 and req.user_id:
        user_hash = int(hashlib.md5(req.user_id.encode()).hexdigest()[:8], 16) / 0xFFFFFFFF
        conv_key = (req.session_id or "") + "|" + (req.conversation_id or "")
        if conv_key.strip("|"):
            lane = int(hashlib.md5(conv_key.encode()).hexdigest()[:8], 16) % _USER_POOL_SIZE
        else:
            lane = random.randrange(_USER_POOL_SIZE)
        position = (user_hash + lane / _USER_POOL_SIZE) % 1.0
        return _walk_cdf(position)

    sticky_key = (req.session_id or "") + "|" + (req.conversation_id or "") + "|model"
    if sticky_key.strip("|"):
        h = int(hashlib.md5(sticky_key.encode()).hexdigest()[:8], 16) / 0xFFFFFFFF
        return _walk_cdf(h)
    return _walk_cdf(random.random())
DEFAULT_KEEP_ALIVE = os.getenv("OLLAMA_KEEP_ALIVE", "30m")
DEFAULT_NUM_CTX = int(os.getenv("OLLAMA_NUM_CTX", "4096"))
DEFAULT_TIMEOUT = float(os.getenv("OLLAMA_TIMEOUT_SECONDS", "120"))


def _to_ollama_messages(messages: list[dict]) -> list[dict]:
    """OpenAI-style messages -> Ollama's `messages` shape.

    Ollama accepts {role, content} pairs natively. We handle a few
    edge cases: `tool` role becomes a `tool` message; `assistant` with
    tool_calls is flattened (Ollama returns tool_calls in its own
    response shape; for input we just pass through what we have).
    """
    out: list[dict] = []
    for msg in messages:
        role = msg.get("role")
        content = msg.get("content")
        if isinstance(content, list):
            # Flatten list-of-dicts content to plain text (Anthropic-style content blocks).
            content = " ".join(
                c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text"
            )
        out.append({"role": role, "content": str(content or "")})
    return out


def _to_ollama_tools(tools: list[dict] | None) -> list[dict] | None:
    """Map our specialist tool schemas (Anthropic-shaped) -> Ollama's tools shape.

    Ollama's `tools` field follows OpenAI's function-calling convention:
        {"type": "function", "function": {"name", "description", "parameters"}}
    """
    if not tools:
        return None
    out: list[dict] = []
    for t in tools:
        if "function" in t:
            # Already OpenAI shape — pass through.
            out.append(t)
            continue
        # Convert from Anthropic shape (used by our specialists' tools.py)
        out.append({
            "type": "function",
            "function": {
                "name": t.get("name", "unknown"),
                "description": t.get("description", ""),
                "parameters": t.get("input_schema", {"type": "object", "properties": {}}),
            },
        })
    return out


def _parse_ollama_tool_calls(message: dict) -> list[dict]:
    """Extract tool_calls from Ollama's response message."""
    tcs = message.get("tool_calls") or []
    out: list[dict] = []
    for tc in tcs:
        fn = tc.get("function") or {}
        out.append({
            "id": tc.get("id") or f"toolu_{len(out):03d}",
            "name": fn.get("name"),
            "input": fn.get("arguments") or {},
        })
    return out


async def generate(req: ProviderRequest, sigil_client: Any) -> ProviderResponse:
    """Call Ollama's /api/chat. Emit Sigil generation. Return normalised response."""
    base_url = os.getenv("OLLAMA_BASE_URL")
    if not base_url:
        raise RuntimeError("OLLAMA_BASE_URL not set")

    model = _pick_model(req)
    payload: dict[str, Any] = {
        "model": model,
        "messages": _to_ollama_messages(req.messages),
        "stream": False,
        "keep_alive": DEFAULT_KEEP_ALIVE,
        "options": {
            "num_ctx": DEFAULT_NUM_CTX,
            "num_predict": min(req.max_tokens or 1024, 2048),
        },
    }
    if req.temperature is not None:
        payload["options"]["temperature"] = req.temperature
    if req.top_p is not None:
        payload["options"]["top_p"] = req.top_p
    tools = _to_ollama_tools(req.tools)
    if tools is not None:
        payload["tools"] = tools

    # Start Sigil generation record
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

    try:
        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
            r = await client.post(f"{base_url.rstrip('/')}/api/chat", json=payload)
            # Ollama returns 400 on "model does not support tools" — try once without tools
            if r.status_code == 400 and tools is not None and "does not support tools" in r.text.lower():
                log.info("model %s doesn't support tools; retrying without", model)
                payload_no_tools = {k: v for k, v in payload.items() if k != "tools"}
                r = await client.post(f"{base_url.rstrip('/')}/api/chat", json=payload_no_tools)
            r.raise_for_status()
            data = r.json()
    except Exception as e:
        if rec is not None:
            try:
                # Attach input messages BEFORE marking the call errored, so
                # Sigil's conversation thread shows what the user/specialist
                # actually asked even when the upstream provider 5xx'd. Without
                # this, errored conversations render as "No messages in this
                # turn" because set_call_error alone carries no payload.
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

    message = data.get("message", {}) or {}
    content_text = message.get("content", "") or ""
    tool_calls = _parse_ollama_tool_calls(message)
    input_tokens = int(data.get("prompt_eval_count") or 0)
    output_tokens = int(data.get("eval_count") or 0)
    response_model = data.get("model") or model

    cost_usd = calculate_cost(PROVIDER_NAME, response_model, input_tokens, output_tokens)
    _attrs = dict(
        provider=PROVIDER_NAME,
        model=response_model or model,
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
                model=ModelRef(provider=PROVIDER_NAME, name=response_model or model),
                response_model=response_model or "",
                stop_reason=data.get("done_reason") or ("stop" if data.get("done") else ""),
                input=_to_sigil_input(req.messages),
                output=output_messages,
                usage=TokenUsage(
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                ),
            ))
            rec.end()
            if rec.err():
                log.warning("sigil generation rec.err(): %s", rec.err())
        except Exception:
            log.exception("sigil set_result/end failed")

    return ProviderResponse(
        content=content_text,
        model=response_model,
        provider=PROVIDER_NAME,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_usd=cost_usd,
        finish_reason=data.get("done_reason"),
        response_id=None,
        generation_id=(getattr(rec, "generation_id", None) or getattr(rec, "id", None)) if rec is not None else None,
        tool_calls=tool_calls,
    )
