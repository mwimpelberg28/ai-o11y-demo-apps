"""Gateway-owned OTel metrics.

sigil-sdk emits gen_ai_client_operation_duration_seconds,
gen_ai_client_token_usage, etc. automatically — but those metrics do
NOT carry a user_id attribute (sigil-sdk's internal metric emission
strips conversation/user fields and keeps only operation_name,
provider, model, agent_name, error_type, error_category).

For "is this AI agent paying for itself per user" and "who's our
heaviest LLM user this hour" questions, we need our own counters
that include user_id. That's what lives in this module.

Naming follows OTel GenAI semantic conventions; OTLP-to-Prom mapping
appends `_total` to monotonic Sum counters.

Metrics emitted from here:
  gen_ai_client_cost_usd_total            — sum, USD per call
  gen_ai_user_calls_total                 — sum, 1 per LLM call (per-user breakdown)
  gen_ai_user_tokens_total                — sum, input + output tokens per user
"""
from __future__ import annotations

from opentelemetry import metrics

_meter = metrics.get_meter(__name__)

# Cumulative spend. Attribute keys mirror sigil-sdk's so cost panels can
# join cleanly against the SDK's duration / token metrics, plus user_id
# for per-user attribution.
cost_usd_counter = _meter.create_counter(
    name="gen_ai.client.cost.usd",
    # No `unit=...` here. Setting unit="USD" causes the OTLP→Prom mapping to
    # append the unit to the metric name, producing the awkward double-suffix
    # `gen_ai_client_cost_usd_USD_total` in Prom. Without unit, we get the
    # clean `gen_ai_client_cost_usd_total`.
    description="Cumulative LLM API spend in USD per provider/model/agent/user.",
)

# Call count per user. sigil-sdk's gen_ai_client_operation_duration_seconds_count
# is the same conceptual count but DOESN'T carry user_id, so it can't answer
# "who are our top 10 LLM users" or "how many calls did mira make today".
user_calls_counter = _meter.create_counter(
    name="gen_ai.user.calls",
    description="LLM calls per user. Increments 1 per /v1/llm call with full user attribution.",
)

# Token count per user. Same gap as above — sigil-sdk's
# gen_ai_client_token_usage_sum doesn't carry user_id.
user_tokens_counter = _meter.create_counter(
    name="gen_ai.user.tokens",
    description="Tokens consumed per user, split by token_type (input | output).",
)

# Provider-fallback counter. Increments every time the primary provider
# raised and we successfully retried on another open provider. Lets the
# dashboard show infra-level flakiness even when the user got a 200.
fallback_counter = _meter.create_counter(
    name="llm_gateway.fallback",
    description="Successful provider fallbacks. Labels: from_provider, to_provider, caller_type.",
)


def record_fallback(*, from_provider: str, to_provider: str, caller_type: str) -> None:
    """Increment fallback counter when one provider failed and another served the call."""
    fallback_counter.add(
        1,
        attributes={
            "from_provider": from_provider,
            "to_provider": to_provider,
            "caller_type": caller_type,
        },
    )


def record_cost(*, provider: str, model: str, agent_name: str, user_id: str,
                session_id: str = "", cost_usd: float) -> None:
    """Record one LLM call's cost with full provider/model/agent/user attribution.

    No-op on zero/negative cost (e.g. Ollama with no per-token pricing
    configured wouldn't add noise to the counter). session_id lets dashboards
    answer "top sessions by spend" — conversation-level granularity that the
    user-level aggregates can't show.
    """
    if cost_usd is None or cost_usd <= 0:
        return
    cost_usd_counter.add(
        cost_usd,
        attributes={
            "gen_ai.system": provider,
            "gen_ai.request.model": model,
            "gen_ai.agent.name": agent_name,
            "user_id": user_id or "unknown",
            "session_id": session_id or "",
        },
    )


def record_user_call(*, provider: str, model: str, agent_name: str, user_id: str) -> None:
    """Increment the per-user call counter by 1."""
    user_calls_counter.add(
        1,
        attributes={
            "gen_ai.system": provider,
            "gen_ai.request.model": model,
            "gen_ai.agent.name": agent_name,
            "user_id": user_id or "unknown",
        },
    )


def record_user_tokens(*, provider: str, model: str, agent_name: str, user_id: str,
                       input_tokens: int, output_tokens: int) -> None:
    """Increment per-user token counters for one LLM call's input + output."""
    common = {
        "gen_ai.system": provider,
        "gen_ai.request.model": model,
        "gen_ai.agent.name": agent_name,
        "user_id": user_id or "unknown",
    }
    if input_tokens > 0:
        user_tokens_counter.add(input_tokens, attributes={**common, "gen_ai.token.type": "input"})
    if output_tokens > 0:
        user_tokens_counter.add(output_tokens, attributes={**common, "gen_ai.token.type": "output"})
