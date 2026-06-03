# Loadgen behavior — ai-o11y-demo-apps

**Status:** Draft v0.1 (2026-05-22).

Defines the synthetic traffic patterns the central K6 loadgen runs against NeonCart and SupportBot. Goal: populate dashboards with **realistic-looking, varied traffic** at ~3-5 LLM calls/minute total. Not a stress test.

Loadgen lives in the `k6-loadgen` namespace. It calls:
- NeonCart at `nc-web.neoncart.svc.cluster.local`
- SupportBot at `sb-web.support-bot.svc.cluster.local`
- Gateway `/open` at `llm-gateway.llm-gateway.svc.cluster.local/open`

## User pool

**NeonCart (200 users):**
- 150 non-AI (75%) — never invoke chatbot or gift-finder
- 50 AI (25%) — split 60/30/10:
  - 30 gift-finder-only
  - 15 chatbot-only
  - 5 both

**SupportBot (~30 users default):** 100% use AI chatbot. Emails default to `<name>@acme.com` (Acme is the fictional SupportBot company).

**NC user emails** sampled from `<name>@gmail.com` / `aol.com` / `yahoo.com` (40/40/20 for variety in domain-cohort dashboards).

User-cohort assignment is **stable per user** (a "gift-finder-only" user always uses the gift-finder, never the chatbot, across all their sessions).

## Username generation

Names are not hardcoded — they're produced by a generation script and checked into `config/users.yaml`:

```bash
# Default: deterministic seed, 200 NC + 30 SB users
python tools/regenerate-users.py --seed 42 > config/users.yaml

# Different seed = fresh user pool
python tools/regenerate-users.py --seed 99 --nc-count 300 --sb-count 50 > config/users.yaml
```

Uses the Python `faker` library for first/last name combos (~20,000 possible combinations — collisions extremely unlikely at this scale).

**`config/users.yaml` is the runtime source of truth.** The loadgen reads it at startup.

To customize:
- **Hand-edit**: open `config/users.yaml`, change a name/email/cohort for specific users (e.g., make user 42 always be `tim.lawrence@acme.com` for the data-theft extension later)
- **Full regen**: rerun the script with a new seed
- **Add a few more**: rerun with a higher count and same seed (existing users stay; new ones appended deterministically)

Hand-edits survive across loadgen restarts; only re-running the regen script overwrites them. Don't auto-regenerate on startup.

## Session arrival

Steady arrival rate, configurable via env vars:
- `NC_SESSIONS_PER_HOUR` (default ~60 → ~1/min)
- `SB_SESSIONS_PER_HOUR` (default ~30 → ~0.5/min)

Each user has a cooldown between sessions (30-60 min, randomized).

**Time-of-day shape** (optional, `LOADGEN_TIME_OF_DAY=true|false`):
- If on: 1.5× rate during simulated business hours (9am-5pm in `LOADGEN_TZ`), 0.5× at night
- If off (default): steady 24/7

## NeonCart non-AI journeys (150 users, weighted)

| Journey | Weight | Steps | Buys? |
|---|---|---|---|
| Quick browser | 40% | land → view 1-2 products → leave | No |
| Searcher | 25% | land → search → view results → maybe click product → maybe buy | 30% buy |
| Browser-shopper | 20% | land → browse categories → view 3-5 products → add 1-2 → maybe checkout | 60% buy |
| Direct purchaser | 10% | land → search specific → add → checkout fast | 90% buy |
| Abandoned cart | 5% | land → add to cart → leave | No |

Each step has variable delay (3-15s) and variable product counts.

## NeonCart AI journeys

### Gift-finder-only users (30 users)

| Journey | Weight | Steps |
|---|---|---|
| Single-shot | 50% | open finder → 1 prompt → browse 3 recs → leave |
| Refining | 25% | open finder → 1 prompt → unsatisfied → refine → browse → leave |
| Converting | 20% | open finder → prompt → browse recs → add to cart → checkout |
| Browse-and-go | 5% | open finder → prompt → look → leave without action |

Prompts sampled from a pool (~20 prompts), e.g.:
- "birthday gift for my dad"
- "anniversary present under $100"
- "gift for a teenager who likes gaming"
- "graduation gift for someone going into engineering"
- "small thoughtful gift for a coworker"

### Chatbot-only users (15 users)

| Journey | Weight | Steps |
|---|---|---|
| Quick Q&A | 40% | open chatbot → 1 question → read answer → close |
| Navigation-driven | 35% | open chatbot → "show me X" → chatbot navigates → user browses → maybe buy |
| Multi-turn | 20% | open chatbot → ask → clarify → follow-up (3-5 turns) → maybe buy |
| Frustrated | 5% | open chatbot → ask → unsatisfied → re-ask differently → still unsatisfied → leave |

Question pool: return policy, shipping, recommendations ("show me running shoes"), price questions, availability, etc.

### Both users (5 users)

Use both features in same session in random order. One gift-finder journey + one chatbot journey. Highest LLM cost per user.

## SupportBot journeys (30+ users, 100% AI)

| Journey | Weight | Steps | Routes to |
|---|---|---|---|
| Billing question | 30% | open → ask billing → answer | sb-billing |
| Tech support — easy | 25% | open → ask tech → answer → close | sb-tech-support |
| Tech support — multi-turn | 15% | open → ask → clarify → follow-up (3-5 turns) | sb-tech-support |
| Account question | 20% | open → ask account → answer | sb-account-management |
| Wrong domain | 5% | open → off-topic → router says "can't help" | (router only) |
| Frustrated | 5% | open → ask → unsatisfied → re-ask → re-ask → leave | varies |

~15 questions per domain, sampled.

## Variation dimensions (make traffic feel non-mechanical)

For every user behavior, randomize:
- **Inter-step delay**: 3-15s uniform, occasional 30s "ponder time"
- **Product browsing count**: 1-7 per session
- **Cart size**: 1-4 items
- **Conversation turn count**: as defined ± 1
- **Session arrival jitter**: ±10% random

## Caller-type tagging (synthetic vs interactive)

All loadgen requests set header `X-Caller-Type: synthetic` when calling NC/SB. The websites propagate this header to the gateway. The gateway uses it to route:

- `X-Caller-Type: synthetic` → random routing across configured providers, respects `/open`, counts against caps
- (no header / `interactive`) → always Claude, ungated, no `/open` check

This is why real-human browser use of the websites never gets throttled — humans don't send the synthetic header, so the gateway routes them straight to Claude.

**Important:** interactive spend STILL counts toward Claude's daily total. So heavy human use can push total Claude spend past the configured cap. Loadgen throttles as the cap approaches, interactive does not. The cap is a soft target on total spend, biased toward serving interactive traffic.

## Loadgen behavior when LLM Gateway throttles

Loadgen polls `/open` every 5s and logs Anthropic open/close transitions for observability, but **does not gate K6 scenarios on cap state**. All scenarios run 24/7.

When Claude closes (cap tripped), individual synthetic requests still flow through the gateway, which falls back to Ollama for synthetic traffic (see `gateway/app/router.py:104-106` — open candidates are re-normalized, so Ollama gets 100% when it's the only one open). The dashboard shows traffic shifting from Anthropic to Ollama rather than disappearing.

Interactive (real-user) traffic is unaffected — it always goes to Claude ungated regardless of cap state.

## Configurables (env vars)

| Env var | Default | Notes |
|---|---|---|
| `NC_TOTAL_USERS` | 200 | NC user pool size |
| `NC_AI_ADOPTION_RATE` | 0.25 | Fraction of NC users that use AI |
| `NC_SESSIONS_PER_HOUR` | 60 | Target session arrival rate |
| `SB_TOTAL_USERS` | 30 | SB user pool size |
| `SB_SESSIONS_PER_HOUR` | 30 | Target session arrival rate |
| `LOADGEN_TIME_OF_DAY` | false | Apply business-hours rate curve |
| `LOADGEN_TZ` | `America/New_York` | TZ for time-of-day curve |
| `LOADGEN_POLL_OPEN_INTERVAL_SEC` | 5 | How often to poll gateway `/open` |
| `LOADGEN_NC_BASE_URL` | (in-cluster default) | Override for cluster-external testing |
| `LOADGEN_SB_BASE_URL` | (in-cluster default) | Override for cluster-external testing |
| `SB_USER_DOMAIN` | `acme.com` | Email domain for SupportBot synthetic users |
| `USERS_CONFIG_PATH` | `/etc/loadgen/users.yaml` | Path to the generated users file |

## Built into the base (not deferred)

- **"Show me mice" trap.** Hardcoded in `apps/neoncart-chatbot/`: when a user message contains "mice" (case-insensitive), the chatbot tries a Postgres query against a non-existent `species` column → DB error → cascading trace. Signature demo moment of AI o11y. Always on. Loadgen scenarios can optionally trigger it occasionally (e.g., 1-2% of nc-chatbot users send "show me mice") to keep the trace populated.

## Out of scope for the BASE (extensions add these later)

- Scripted bad actors (Tim-as-data-thief, etc.)
- Outlier traffic spikes
- Domain-specific bursts
- Adversarial prompts / prompt injection
- High-volume stress patterns
- Other specific failure-mode triggers
