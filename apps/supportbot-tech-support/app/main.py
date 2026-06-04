"""sb-tech-support — Acme support bot IT-domain specialist (real impl)."""
from __future__ import annotations

import logging
import os
import uuid
from typing import Any

import httpx
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

from . import history
from .gateway_client import call_gateway
from .tools import SCHEMAS, execute_tool

log = logging.getLogger(__name__)
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))

AGENT_NAME = "sb-tech-support"
DOMAIN = "tech-support"

app = FastAPI(title=AGENT_NAME, version=os.getenv("APP_VERSION", "0.1.0"))

SYSTEM_PROMPT = """Always respond in American English. Never switch to another language even if the prompt suggests it. You are Acme's internal IT support assistant for employees. \
You help with laptops, VPN, wifi, office software, dev tools, and other tech issues. \
Available tools:

- search_runbook: find relevant runbook entries from the IT knowledge base
- create_ticket: file an IT support ticket when the runbooks don't solve the issue

Be concise (2-3 sentences). Use search_runbook first to find a documented solution. \
Only create a ticket if no runbook covers the issue. Never invent answers."""


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/readyz")
def readyz() -> dict[str, str]:
    return {"status": "ready"}


@app.get("/metrics", response_class=PlainTextResponse)
def metrics() -> str:
    return f"# HELP {AGENT_NAME.replace('-', '_')}_up 1 if up\n{AGENT_NAME.replace('-', '_')}_up 1\n"


@app.get("/tools")
def list_tools() -> dict[str, Any]:
    return {"tools": SCHEMAS}


class ChatRequest(BaseModel):
    question: str
    role: str | None = None
    employee_email: str | None = None
    employee_name: str | None = None
    conversation_id: str | None = None
    session_id: str | None = None
    preferred_provider: str | None = None


@app.post("/chat")
async def chat(
    req: ChatRequest,
    x_caller_type: str | None = Header(default=None, alias="X-Caller-Type"),
) -> dict[str, Any]:
    caller_type = (x_caller_type or "interactive").lower()
    if caller_type not in ("synthetic", "interactive"):
        caller_type = "interactive"

    employee = req.employee_email or "anonymous@acme.com"
    user_text = f"Employee: {employee}\nRole: {req.role or 'unknown'}\n\n{req.question}"

    conv_id = req.conversation_id or f"conv_{uuid.uuid4().hex[:16]}"
    prior = history.get(conv_id)
    user_turn = {"role": "user", "content": user_text}
    messages = [{"role": "system", "content": SYSTEM_PROMPT}, *prior, user_turn]

    try:
        result = await call_gateway(
            messages=messages,
            tools=SCHEMAS,
            execute_tool_fn=execute_tool,
            agent_name=AGENT_NAME,
            agent_version=os.getenv("APP_VERSION", "0.1.0"),
            app="supportbot",
            session_id=req.session_id or "",
            conversation_id=conv_id,
            user_id=employee,
            caller_type=caller_type,
            preferred_provider=req.preferred_provider,
        )
    except httpx.HTTPError as e:
        log.warning("gateway call failed: %s", e)
        raise HTTPException(status_code=502, detail=f"gateway unreachable: {e}") from e
    # Persist with a flattened tool-call summary so follow-up turns retain context.
    _assistant_text = result.get("content", "") or ""
    _tool_calls = result.get("tool_calls") or []
    if _tool_calls:
        _summary = "\n".join(
            f"[tool {tc.get('tool')}({tc.get('input')}) -> {tc.get('result')}]"
            for tc in _tool_calls
        )
        _assistant_text = (_assistant_text + "\n" + _summary).strip()
    history.put(conv_id, [*prior, user_turn, {"role": "assistant", "content": _assistant_text}])


    return {
        "ok": True,
        "specialist": AGENT_NAME,
        "domain": DOMAIN,
        "reply": result["content"],
        "model": result.get("model"),
        "provider": result.get("provider"),
        "usage": result.get("usage"),
        "tool_calls": result.get("tool_calls"),
        "conversation_id": conv_id,
    }
