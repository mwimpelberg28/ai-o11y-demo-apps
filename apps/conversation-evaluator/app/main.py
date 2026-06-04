"""Conversation Evaluator.

A small FastAPI service that periodically scores the highest-volume
SupportBot conversations with an Ollama-driven evaluator, separate from
the llm-gateway routing (no cap, no admission control).

Behavior:
  1. Poll the gateway's `/api/sessions/recent` endpoint every
     POLL_INTERVAL_SEC seconds, filtering to acme.com employees and
     sorting by output tokens (the metric most affected by the demo's
     anomaly bursts).
  2. For each of the top TOP_N sessions, GET its latest turn from the
     gateway, then call Ollama directly with the evaluator prompt.
  3. Parse the JSON `{"score": N, "reason": "..."}` and emit a
     `conversation_eval_score` gauge with session_id / user_id /
     conversation_id / evaluator_model labels.
  4. Skip sessions whose call-count hasn't changed since the last
     evaluation (avoid re-scoring quiet sessions).

Env:
    GATEWAY_URL              default http://llm-gateway.llm-gateway.svc.cluster.local:8000
    OLLAMA_BASE_URL          required — e.g. http://ollama.ollama.svc.cluster.local:11434
    EVAL_MODEL               default qwen2.5:14b
    POLL_INTERVAL_SEC        default 60
    TOP_N                    default 5
    USER_FILTER              default @acme.com
    SORT_BY                  default output_tokens
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, AsyncIterator

import httpx
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from opentelemetry import metrics

log = logging.getLogger(__name__)
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))


# ── Settings ──────────────────────────────────────────────────────────────────

@dataclass
class Settings:
    gateway_url: str
    ollama_url: str
    eval_model: str
    poll_interval_sec: float
    top_n: int
    user_filter: str
    sort_by: str
    ollama_timeout: float

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            gateway_url=os.getenv(
                "GATEWAY_URL",
                "http://llm-gateway.llm-gateway.svc.cluster.local:8000",
            ).rstrip("/"),
            ollama_url=os.getenv("OLLAMA_BASE_URL", "").rstrip("/"),
            eval_model=os.getenv("EVAL_MODEL", "qwen2.5:14b"),
            poll_interval_sec=float(os.getenv("POLL_INTERVAL_SEC", "60")),
            top_n=int(os.getenv("TOP_N", "5")),
            user_filter=os.getenv("USER_FILTER", "@acme.com"),
            sort_by=os.getenv("SORT_BY", "output_tokens"),
            ollama_timeout=float(os.getenv("OLLAMA_TIMEOUT_SEC", "120")),
        )


# ── Metric (OTel gauge) ───────────────────────────────────────────────────────

_meter = metrics.get_meter("conversation_evaluator")
# Use an observable gauge backed by an in-process dict so we can update it
# from the poll loop and the OTel SDK will emit on each collection cycle.
_score_state: dict[tuple[str, str, str, str, str], float] = {}
_reason_state: dict[tuple, str] = {}


def _score_callback(_options):
    from opentelemetry.metrics import Observation
    for (session_id, conv_id, user_id, agent_name, model), score in _score_state.items():
        yield Observation(
            score,
            attributes={
                "session_id": session_id,
                "conversation_id": conv_id,
                "user_id": user_id,
                "gen_ai.agent.name": agent_name,
                "evaluator_model": model,
            },
        )


_meter.create_observable_gauge(
    "conversation_eval_score",
    callbacks=[_score_callback],
    description="0-100 AI-usage value score per conversation. Higher = better use of AI.",
)


# ── Evaluator prompt ──────────────────────────────────────────────────────────

EVAL_SYSTEM_PROMPT = (
    "You evaluate how valuable and well-suited the AI was for the task in "
    "this conversation. Consider what the user was trying to accomplish, "
    "the complexity of the task, and whether AI was an efficient choice "
    "for it. Be strict. If uncertain, choose the lower score."
)

EVAL_USER_PROMPT_TMPL = """Latest user message:
{latest_user_message}

Assistant response:
{assistant_response}

Score the AI usage value 0-100 using these anchors:

    0  = wasteful or trivial (e.g., uploading a full document to extract one sentence; asking something a Ctrl+F handles)
   25  = low value (single-fact lookup; something a search engine answers faster)
   50  = acceptable (summarization, basic explanation, light editing)
   75  = good use: multi-step reasoning, debugging, or synthesis across sources
  100  = high value: building something net-new or solving a complex problem end-to-end (e.g., generating an app, diagnosing a production incident, writing and iterating on code)

Return JSON: {{"score": <0-100 integer>, "reason": "<one short sentence>"}}"""


# Regex fallback for when the model wraps JSON in chatter.
_JSON_RE = re.compile(r"\{[^{}]*\"score\"\s*:\s*\d+[^{}]*\}", re.DOTALL)


async def evaluate_with_ollama(settings: Settings, client: httpx.AsyncClient,
                                latest_user_message: str,
                                assistant_response: str) -> tuple[int, str] | None:
    """Call Ollama and return (score, reason), or None on failure."""
    user_prompt = EVAL_USER_PROMPT_TMPL.format(
        latest_user_message=latest_user_message[:4000],
        assistant_response=assistant_response[:4000],
    )
    body = {
        "model": settings.eval_model,
        "messages": [
            {"role": "system", "content": EVAL_SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        "stream": False,
        "format": "json",  # Ollama returns valid JSON-only when this is set.
        "options": {"temperature": 0.0, "num_predict": 256},
    }
    try:
        r = await client.post(
            f"{settings.ollama_url}/api/chat",
            json=body,
            timeout=settings.ollama_timeout,
        )
        r.raise_for_status()
        data = r.json()
    except httpx.HTTPError as e:
        log.warning("ollama eval call failed: %s", e)
        return None

    raw = ((data.get("message") or {}).get("content") or "").strip()
    if not raw:
        log.warning("ollama returned empty content")
        return None
    parsed = _parse_score_json(raw)
    if parsed is None:
        log.warning("could not parse ollama eval output: %r", raw[:200])
        return None
    return parsed


def _parse_score_json(raw: str) -> tuple[int, str] | None:
    candidates: list[str] = [raw]
    m = _JSON_RE.search(raw)
    if m:
        candidates.append(m.group(0))
    for c in candidates:
        try:
            d = json.loads(c)
        except json.JSONDecodeError:
            continue
        try:
            score = int(d.get("score"))
            reason = str(d.get("reason") or "").strip()
        except (TypeError, ValueError):
            continue
        score = max(0, min(100, score))
        return score, reason
    return None


# ── Polling loop ──────────────────────────────────────────────────────────────

class Evaluator:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        # session_id -> last_evaluated_call_count (so we re-eval only when the
        # session keeps growing)
        self._last_eval_calls: dict[str, int] = {}
        self._stop = asyncio.Event()
        self._cycle_count = 0
        self._scores_emitted = 0
        self._evals_attempted = 0
        self._evals_failed = 0

    def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        if not self.settings.ollama_url:
            log.error("OLLAMA_BASE_URL not set; evaluator will idle")
        log.info(
            "evaluator polling gateway=%s ollama=%s model=%s every %.1fs (top=%d)",
            self.settings.gateway_url, self.settings.ollama_url,
            self.settings.eval_model, self.settings.poll_interval_sec, self.settings.top_n,
        )
        async with httpx.AsyncClient() as client:
            while not self._stop.is_set():
                try:
                    await self._one_cycle(client)
                except Exception:
                    log.exception("eval cycle errored")
                try:
                    await asyncio.wait_for(self._stop.wait(),
                                           timeout=self.settings.poll_interval_sec)
                except asyncio.TimeoutError:
                    pass

    async def _one_cycle(self, client: httpx.AsyncClient) -> None:
        self._cycle_count += 1
        if not self.settings.ollama_url:
            return
        # 1. Fetch top-N recent sessions
        url = f"{self.settings.gateway_url}/api/sessions/recent"
        params = {
            "limit": self.settings.top_n,
            "sort": self.settings.sort_by,
            "user_filter": self.settings.user_filter,
        }
        try:
            r = await client.get(url, params=params, timeout=10.0)
            r.raise_for_status()
            payload = r.json()
        except httpx.HTTPError as e:
            log.warning("gateway recent-sessions failed: %s", e)
            return
        sessions = payload.get("sessions") or []
        log.info("cycle=%d tracking=%d candidates=%d",
                 self._cycle_count, payload.get("total_tracked", 0), len(sessions))

        # 2. Evaluate each (skip already-evaluated unchanged ones)
        for s in sessions:
            sid = s.get("session_id")
            if not sid:
                continue
            calls = int(s.get("calls") or 0)
            if self._last_eval_calls.get(sid) == calls:
                continue
            await self._evaluate_one(client, sid, s)
            self._last_eval_calls[sid] = calls

    async def _evaluate_one(self, client: httpx.AsyncClient, sid: str,
                             summary: dict[str, Any]) -> None:
        self._evals_attempted += 1
        try:
            r = await client.get(
                f"{self.settings.gateway_url}/api/sessions/{sid}/latest-turn",
                timeout=10.0,
            )
            if r.status_code == 404:
                return
            r.raise_for_status()
            turn = r.json()
        except httpx.HTTPError as e:
            log.warning("latest-turn fetch failed for %s: %s", sid, e)
            self._evals_failed += 1
            return

        user_msg = turn.get("latest_user_message") or ""
        asst_msg = turn.get("assistant_response") or ""
        if not user_msg or not asst_msg:
            log.info("session %s has incomplete turn; skipping", sid)
            return

        result = await evaluate_with_ollama(
            self.settings, client, user_msg, asst_msg,
        )
        if result is None:
            self._evals_failed += 1
            return
        score, reason = result

        key = (
            sid,
            turn.get("conversation_id") or summary.get("conversation_id") or "",
            turn.get("user_id") or summary.get("user_id") or "",
            turn.get("agent_name") or summary.get("agent_name") or "",
            self.settings.eval_model,
        )
        _score_state[key] = float(score)
        _reason_state[key] = reason
        self._scores_emitted += 1
        log.info("scored session=%s user=%s score=%d reason=%r",
                 sid, key[2], score, reason[:120])

    def status(self) -> dict[str, Any]:
        return {
            "cycle_count": self._cycle_count,
            "evals_attempted": self._evals_attempted,
            "evals_failed": self._evals_failed,
            "scores_emitted_in_metric": self._scores_emitted,
            "active_scores": len(_score_state),
            "settings": {
                "ollama_url": self.settings.ollama_url,
                "eval_model": self.settings.eval_model,
                "top_n": self.settings.top_n,
                "poll_interval_sec": self.settings.poll_interval_sec,
                "user_filter": self.settings.user_filter,
                "sort_by": self.settings.sort_by,
            },
        }


# ── FastAPI app ───────────────────────────────────────────────────────────────

_evaluator: Evaluator | None = None
_poll_task: asyncio.Task | None = None


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    global _evaluator, _poll_task
    _evaluator = Evaluator(Settings.from_env())
    _poll_task = asyncio.create_task(_evaluator.run())
    log.info("conversation-evaluator started")
    try:
        yield
    finally:
        log.info("conversation-evaluator shutting down")
        if _evaluator is not None:
            _evaluator.stop()
        if _poll_task is not None:
            try:
                await asyncio.wait_for(_poll_task, timeout=10)
            except asyncio.TimeoutError:
                _poll_task.cancel()


app = FastAPI(lifespan=lifespan, title="conversation-evaluator")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/readyz")
def readyz() -> JSONResponse:
    if _evaluator is None:
        return JSONResponse(status_code=503, content={"status": "starting"})
    return JSONResponse(content={"status": "ready"})


@app.get("/status")
def status() -> dict[str, Any]:
    if _evaluator is None:
        return {"status": "starting"}
    return _evaluator.status()


@app.get("/scores")
def scores() -> dict[str, Any]:
    """List the current cached scores. Useful for debugging without Grafana."""
    out = []
    for key, score in _score_state.items():
        sid, conv, user, agent, model = key
        out.append({
            "session_id": sid,
            "conversation_id": conv,
            "user_id": user,
            "agent_name": agent,
            "evaluator_model": model,
            "score": score,
            "reason": _reason_state.get(key, ""),
        })
    out.sort(key=lambda x: x["score"], reverse=True)
    return {"scores": out, "count": len(out)}
