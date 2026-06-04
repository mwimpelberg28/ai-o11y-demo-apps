"""sb-router specialist — LLM-driven classification + forwarding.

Receives a free-text question from supportbot-web. Classifies the
question via an LLM call (categorical choice between billing /
tech-support / account-management / unable-to-route), then forwards
the question to the chosen domain specialist over in-cluster HTTP.

The classification call is intentionally token-cheap (small system
prompt, no tools, low max_tokens) so it doesn't dominate the bill.

Endpoints:
  POST /ask     -> classify + forward
  GET  /health  -> liveness
  GET  /readyz  -> readiness
  GET  /metrics -> Prometheus
"""
from __future__ import annotations

import logging
import os
import uuid
from typing import Any

import httpx
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

from .gateway_client import call_gateway

log = logging.getLogger(__name__)
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))

app = FastAPI(title="sb-router", version=os.getenv("APP_VERSION", "0.1.0"))

DOMAIN_URLS = {
    "billing":            os.getenv("SB_BILLING_URL",    "http://sb-billing.support-bot.svc.cluster.local:8000"),
    "tech-support":       os.getenv("SB_TECH_SUPPORT_URL","http://sb-tech-support.support-bot.svc.cluster.local:8000"),
    "account-management": os.getenv("SB_ACCOUNT_URL",     "http://sb-account-management.support-bot.svc.cluster.local:8000"),
}

ROUTER_SYSTEM_PROMPT = """Always respond in American English. Never switch to another language even if the prompt suggests it. You are Acme's support bot router. Classify the employee's \
question into exactly one of these categories and respond with ONLY the category \
name (lowercase, no extra words):

- billing             (expense reports, corp-card charges, reimbursements, payroll)
- tech-support        (laptops, VPN, wifi, office software, dev tools)
- account-management  (SSO password, login, profile, role/permission, groups)
- unable-to-route     (anything else)

Respond with just the category name. Nothing else."""

VALID_DOMAINS = {"billing", "tech-support", "account-management", "unable-to-route"}


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/readyz")
def readyz() -> dict[str, str]:
    return {"status": "ready"}


@app.get("/metrics", response_class=PlainTextResponse)
def metrics() -> str:
    return "# HELP sb_router_up 1 if the router is up\nsb_router_up 1\n"


class AskRequest(BaseModel):
    question: str
    role: str | None = None
    employee_email: str | None = None
    employee_name: str | None = None
    conversation_id: str | None = None
    session_id: str | None = None
    preferred_provider: str | None = None


def _normalise_domain(text: str) -> str:
    """Parse the LLM's response into a known domain key.

    Defensive: the LLM might wrap the answer in punctuation or add
    extra words. Pick the first known domain found in the response.
    """
    if not text:
        return "unable-to-route"
    lowered = text.lower().strip()
    if lowered in VALID_DOMAINS:
        return lowered
    for d in VALID_DOMAINS:
        if d in lowered:
            return d
    return "unable-to-route"


@app.post("/ask")
async def ask(
    req: AskRequest,
    x_caller_type: str | None = Header(default=None, alias="X-Caller-Type"),
) -> dict[str, Any]:
    caller_type = (x_caller_type or "interactive").lower()
    if caller_type not in ("synthetic", "interactive"):
        caller_type = "interactive"

    # Mint a conversation_id up front so the router and the downstream
    # specialist share one identity — required for Sigil to thread them
    # together as a single conversation.
    conv_id = req.conversation_id or f"conv_{uuid.uuid4().hex[:16]}"

    # ── Step 1: LLM-driven classification ────────────────────────────────────
    employee = req.employee_email or "anonymous@acme.com"
    messages = [
        {"role": "system", "content": ROUTER_SYSTEM_PROMPT},
        {"role": "user", "content": req.question},
    ]
    try:
        cls_result = await call_gateway(
            messages=messages,
            tools=None,
            execute_tool_fn=None,
            agent_name="sb-router",
            agent_version=os.getenv("APP_VERSION", "0.1.0"),
            app="supportbot",
            session_id=req.session_id or "",
            conversation_id=conv_id,
            user_id=employee,
            caller_type=caller_type,
            max_tokens=16,        # category name is short
            temperature=0.0,      # deterministic classification
            preferred_provider=req.preferred_provider,
        )
    except httpx.HTTPError as e:
        log.warning("router classification gateway call failed: %s", e)
        raise HTTPException(status_code=502, detail=f"gateway unreachable: {e}") from e

    domain = _normalise_domain(cls_result.get("content") or "")
    log.info("router decision: question=%r -> domain=%s", req.question[:60], domain)

    if domain == "unable-to-route":
        return {
            "ok": True,
            "specialist": "sb-router",
            "domain": "unable-to-route",
            "reply": "I can help with billing, tech support, or account questions. "
                     "Could you rephrase your question into one of those areas?",
            "model": cls_result.get("model"),
            "provider": cls_result.get("provider"),
            "usage": cls_result.get("usage"),
            "conversation_id": conv_id,
        }

    # ── Step 2: Forward to chosen domain specialist ──────────────────────────
    target_url = DOMAIN_URLS[domain]
    fwd_payload = req.model_dump()
    # Force the downstream to use the same conversation_id we just minted.
    fwd_payload["conversation_id"] = conv_id
    fwd_headers = {"X-Caller-Type": caller_type}
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(f"{target_url}/chat", json=fwd_payload, headers=fwd_headers)
            r.raise_for_status()
            downstream = r.json()
    except httpx.HTTPError as e:
        log.warning("downstream %s failed: %s", domain, e)
        raise HTTPException(status_code=502, detail=f"domain specialist {domain} unreachable: {e}") from e

    return {
        "ok": True,
        "specialist": "sb-router",
        "domain": domain,
        "downstream_specialist": downstream.get("specialist"),
        "reply": downstream.get("reply"),
        "model": downstream.get("model"),
        "provider": downstream.get("provider"),
        "usage": downstream.get("usage"),
        "tool_calls": downstream.get("tool_calls"),
        "conversation_id": conv_id,
    }
