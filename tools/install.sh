#!/usr/bin/env bash
# Interactive installer for ai-o11y-demo-apps.
#
# Walks the user through collecting credentials, writing a .env file,
# generating the synthetic-user pool, and (eventually) running `helm install`.
#
# Safe to re-run: existing .env values are reused as defaults, and
# already-generated user pools are not re-rolled unless missing.
set -euo pipefail

# ── Paths ─────────────────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
ENV_FILE="${REPO_ROOT}/.env"
HELM_DIR="${REPO_ROOT}/helm"
USERS_FILE="${HELM_DIR}/config/users.yaml"   # chart reads via .Files.Get "config/users.yaml"
VALUES_OVERRIDE="${REPO_ROOT}/.helm-values-overrides.yaml"  # gitignored

# ── Pretty output ─────────────────────────────────────────────────────────────
# No emojis or color codes — keeps logs greppable in CI / pasted into tickets.
say()  { printf '%s\n' "$*"; }
hdr()  { printf '\n=== %s ===\n' "$*"; }
ok()   { printf '  [OK] %s\n' "$*"; }
warn() { printf '  [WARN] %s\n' "$*" >&2; }
err()  { printf '  [ERROR] %s\n' "$*" >&2; }
die()  { err "$*"; exit 1; }

# ── Prereqs ───────────────────────────────────────────────────────────────────
check_prereqs() {
  hdr "Checking prerequisites"
  local missing=()
  for cmd in kubectl helm python3; do
    if command -v "$cmd" >/dev/null 2>&1; then
      ok "$cmd found: $(command -v "$cmd")"
    else
      missing+=("$cmd")
    fi
  done
  if [[ ${#missing[@]} -gt 0 ]]; then
    die "Missing required commands: ${missing[*]}. Install them and re-run."
  fi

  # Verify Python packages used by regenerate-users.py — fail fast here, not
  # mid-install after the user has filled in 8 credential prompts.
  local missing_py=()
  for pkg in faker yaml; do
    if ! python3 -c "import ${pkg}" >/dev/null 2>&1; then
      missing_py+=("$pkg")
    fi
  done
  if [[ ${#missing_py[@]} -gt 0 ]]; then
    err "Missing Python packages: ${missing_py[*]}"
    say "  Install with:"
    say "      pip install -r tools/requirements.txt"
    say "  Or in a venv (recommended):"
    say "      python3 -m venv .venv && source .venv/bin/activate"
    say "      pip install -r tools/requirements.txt"
    die "Re-run install.sh after the packages are installed."
  fi
  ok "Python packages found: faker, pyyaml"

  # Verify the current kubeconfig works
  if ! kubectl cluster-info >/dev/null 2>&1; then
    die "kubectl can't reach a cluster. Set KUBECONFIG or kubectl config use-context <ctx>."
  fi
  # current-context isn't always set (e.g. direct token auth, in-cluster
  # service account). cluster-info already proved we can reach the API.
  local ctx
  ctx=$(kubectl config current-context 2>/dev/null || echo "<unset, using direct kubeconfig>")
  ok "kubectl context: ${ctx}"
}

# ── Prompt helpers ────────────────────────────────────────────────────────────
# Read a value with a default. If the env file already has the key, use that.
# Usage: prompt_value VAR_NAME "Human prompt" "default"
prompt_value() {
  local var_name="$1"
  local prompt="$2"
  local default="${3:-}"

  local existing="${!var_name:-}"
  local effective_default="${existing:-$default}"

  local prompt_suffix=""
  if [[ -n "$effective_default" ]]; then
    # Mask anything that looks secret
    case "$var_name" in
      *KEY*|*TOKEN*|*SECRET*) prompt_suffix=" [default: ****]";;
      *) prompt_suffix=" [default: ${effective_default}]";;
    esac
  fi

  local input
  read -r -p "${prompt}${prompt_suffix}: " input
  printf -v "$var_name" '%s' "${input:-$effective_default}"
}

# Required field: keep prompting until non-empty
prompt_required() {
  local var_name="$1"
  local prompt="$2"
  local default="${3:-}"
  while true; do
    prompt_value "$var_name" "$prompt" "$default"
    if [[ -n "${!var_name}" ]]; then
      return 0
    fi
    warn "${var_name} is required."
  done
}

# Validate by regex; re-prompt on mismatch
prompt_pattern() {
  local var_name="$1"
  local prompt="$2"
  local pattern="$3"
  local hint="$4"
  local default="${5:-}"
  while true; do
    prompt_required "$var_name" "$prompt" "$default"
    if [[ "${!var_name}" =~ $pattern ]]; then
      return 0
    fi
    warn "Value doesn't match expected format. ${hint}"
    unset "$var_name"
  done
}

# Pre-load any values already set in .env so they show as defaults
load_existing_env() {
  if [[ -f "$ENV_FILE" ]]; then
    say "Reusing existing values from ${ENV_FILE} as defaults."
    # shellcheck disable=SC1090
    set -a; source "$ENV_FILE"; set +a
  fi
}

# ── Collect credentials ───────────────────────────────────────────────────────
collect_required() {
  hdr "Required credentials"

  prompt_pattern CLAUDE_API_KEY \
    "Claude API key (Anthropic)" \
    '^sk-ant-' \
    "Expected format: starts with 'sk-ant-'."

  prompt_pattern SIGIL_ENDPOINT \
    "SIGIL_ENDPOINT (e.g. https://sigil-prod-us-east-0.grafana.net)" \
    '^https?://' \
    "Expected a URL starting with http:// or https://."

  prompt_pattern SIGIL_AUTH_TENANT_ID \
    "SIGIL_AUTH_TENANT_ID (numeric)" \
    '^[0-9]+$' \
    "Expected numeric tenant id."

  prompt_pattern SIGIL_AUTH_TOKEN \
    "SIGIL_AUTH_TOKEN (Cloud Access Policy token)" \
    '^glc_' \
    "Expected format: starts with 'glc_'."

  prompt_pattern OTEL_EXPORTER_OTLP_ENDPOINT \
    "OTEL_EXPORTER_OTLP_ENDPOINT (from the OpenTelemetry card in Grafana Cloud)" \
    '^https?://' \
    "Expected a URL starting with http:// or https://."

  prompt_pattern OTEL_OTLP_INSTANCE_ID \
    "OTEL_OTLP_INSTANCE_ID (numeric — the OTLP card's instance ID, may differ from tenant id)" \
    '^[0-9]+$' \
    "Expected numeric instance id."

  # OTLP token — Grafana Cloud stacks issue a SEPARATE Cloud Access Policy
  # token for OTLP ingestion (metrics + logs + traces) vs Sigil's plugin token.
  # Paste the token from the "Send to OpenTelemetry" / OTLP card.
  #
  # How to find it: in Grafana Cloud → Connections → Add new connection →
  # OpenTelemetry → the "Password / API Token" field on the OTLP card. This
  # is distinct from the Sigil plugin's token; do not reuse the Sigil token
  # unless you have explicitly granted it metrics:write scope.
  say ""
  say "─── OTLP write token ───"
  say "Paste the OTLP write token from Grafana Cloud's OpenTelemetry card."
  say "(Distinct from the Sigil token. Type 'reuse' only if you have"
  say " confirmed your Sigil token has metrics:write scope.)"
  prompt_value OTEL_OTLP_TOKEN "OTEL_OTLP_TOKEN" "${OTEL_OTLP_TOKEN:-}"
  if [ -z "${OTEL_OTLP_TOKEN:-}" ]; then
    die "OTEL_OTLP_TOKEN is required. Get it from Grafana Cloud → Connections → OpenTelemetry."
  elif [ "${OTEL_OTLP_TOKEN}" = "reuse" ]; then
    OTEL_OTLP_TOKEN="${SIGIL_AUTH_TOKEN}"
    ok "Reusing SIGIL_AUTH_TOKEN for OTLP (per explicit 'reuse' input)."
  else
    ok "Using separate OTLP token."
  fi

  # Constants (not prompted, but written to .env so customer can see them)
  SIGIL_PROTOCOL="${SIGIL_PROTOCOL:-http}"
  SIGIL_AUTH_MODE="${SIGIL_AUTH_MODE:-basic}"
}

collect_optional() {
  hdr "Optional credentials (press Enter to skip)"
  prompt_value OPENAI_API_KEY "OpenAI API key" "${OPENAI_API_KEY:-}"
  prompt_value GEMINI_API_KEY "Gemini API key" "${GEMINI_API_KEY:-}"
  prompt_value OLLAMA_BASE_URL "Ollama base URL (e.g. http://ollama.ollama:11434)" "${OLLAMA_BASE_URL:-}"
  say ""
  say "─── Auto-import dashboards (optional) ───"
  say "Provide a Grafana stack URL + service-account token (glsa_*) with"
  say "dashboards:write to have install.sh import the 4 dashboards in dashboards/"
  say "at the end. Skip both prompts to import manually later."
  prompt_value GRAFANA_URL "Grafana stack URL (e.g. https://acme.grafana.net)" "${GRAFANA_URL:-}"
  prompt_value GRAFANA_API_TOKEN "Grafana service-account token (glsa_...)" "${GRAFANA_API_TOKEN:-}"
}

collect_ingress() {
  hdr "Optional Ingress (press Enter to skip — apps remain reachable via kubectl port-forward)"
  say "If you have an Ingress controller (Traefik, nginx, ...) and DNS pointing at it,"
  say "supply public hostnames here. The chart creates one Ingress per host you provide."
  prompt_value INGRESS_CLASS_NAME "Ingress class (e.g. traefik, nginx)" "${INGRESS_CLASS_NAME:-}"
  prompt_value INGRESS_NEONCART_HOST "Public hostname for NeonCart (e.g. neoncart.example.com)" "${INGRESS_NEONCART_HOST:-}"
  prompt_value INGRESS_SUPPORTBOT_HOST "Public hostname for SupportBot (e.g. supportbot.example.com)" "${INGRESS_SUPPORTBOT_HOST:-}"
}

collect_tunables() {
  hdr "Tunables (defaults are fine for most installs)"
  prompt_value ANTHROPIC_CAP_USD_PER_DAY "Anthropic spend cap per day, USD" "${ANTHROPIC_CAP_USD_PER_DAY:-30}"
  prompt_value NC_AI_ADOPTION_RATE "NC AI adoption rate (0-1)" "${NC_AI_ADOPTION_RATE:-0.25}"
  prompt_value NC_TOTAL_USERS "NC total users" "${NC_TOTAL_USERS:-200}"
  prompt_value SB_TOTAL_USERS "SB total users" "${SB_TOTAL_USERS:-30}"
  say ""
  say "Cross-provider traffic split (synthetic loadgen only — interactive traffic is always Anthropic)."
  say "Format: 'anthropic:N,ollama:N'. Examples:"
  say "  anthropic:1,ollama:2   tilt 67% toward Ollama (default — cheaper)"
  say "  anthropic:1,ollama:1   uniform 50/50"
  say "  anthropic:3,ollama:1   Anthropic-heavy (~75%)"
  say "  (empty)                fall back to chart default (anthropic:1,ollama:2)"
  prompt_value PROVIDER_WEIGHTS "PROVIDER_WEIGHTS" "${PROVIDER_WEIGHTS:-}"
}

# ── Compute OTEL_EXPORTER_OTLP_HEADERS ────────────────────────────────────────
# tr -d '\n' is critical: a trailing newline silently breaks the header.
compute_otel_headers() {
  local b64
  b64=$(printf '%s' "${OTEL_OTLP_INSTANCE_ID}:${OTEL_OTLP_TOKEN}" | base64 | tr -d '\n')
  OTEL_EXPORTER_OTLP_HEADERS="Authorization=Basic ${b64}"
}

# ── Alloy detection ───────────────────────────────────────────────────────────
detect_alloy() {
  hdr "Looking for Grafana Alloy in your cluster"
  if kubectl get svc -A 2>/dev/null | grep -q -i alloy; then
    local alloy_lines
    alloy_lines=$(kubectl get svc -A 2>/dev/null | grep -i alloy || true)
    say "Found Alloy services:"
    printf '%s\n' "$alloy_lines"
    say ""
    say "If you'd prefer to route OTLP through your Alloy instance instead of"
    say "shipping directly to Grafana Cloud, paste the in-cluster URL here."
    say "Example: http://alloy.monitoring.svc.cluster.local:4318"
    local override
    read -r -p "Alloy OTLP URL (Enter to skip): " override
    if [[ -n "$override" ]]; then
      OTEL_EXPORTER_OTLP_ENDPOINT="$override"
      ok "Will use Alloy at: ${OTEL_EXPORTER_OTLP_ENDPOINT}"
    else
      ok "Skipped — shipping OTLP straight to Grafana Cloud."
    fi
  else
    ok "No Alloy detected — shipping OTLP straight to Grafana Cloud."
  fi
}

# ── Write .env ────────────────────────────────────────────────────────────────
write_env_file() {
  hdr "Writing ${ENV_FILE}"
  # Quote every value so values with spaces / special chars survive sourcing.
  cat >"$ENV_FILE" <<EOF
# Generated by tools/install.sh on $(date -u +%Y-%m-%dT%H:%M:%SZ)
# Edit by hand; rerun install.sh and existing values will be reused as defaults.

# ── LLM provider credentials ─────────────────────────────────────────────────
CLAUDE_API_KEY="${CLAUDE_API_KEY}"
OPENAI_API_KEY="${OPENAI_API_KEY:-}"
GEMINI_API_KEY="${GEMINI_API_KEY:-}"
OLLAMA_BASE_URL="${OLLAMA_BASE_URL:-}"

# ── Sigil ────────────────────────────────────────────────────────────────────
SIGIL_ENDPOINT="${SIGIL_ENDPOINT}"
SIGIL_PROTOCOL="${SIGIL_PROTOCOL}"
SIGIL_AUTH_MODE="${SIGIL_AUTH_MODE}"
SIGIL_AUTH_TENANT_ID="${SIGIL_AUTH_TENANT_ID}"
SIGIL_AUTH_TOKEN="${SIGIL_AUTH_TOKEN}"

# ── OTLP ─────────────────────────────────────────────────────────────────────
# OTEL_OTLP_TOKEN can be the same as SIGIL_AUTH_TOKEN or a separate token —
# stacks that use distinct Cloud Access Policies for Sigil ingest vs OTLP
# ingest need two tokens. OTEL_EXPORTER_OTLP_HEADERS is base64(instance_id:token).
OTEL_EXPORTER_OTLP_ENDPOINT="${OTEL_EXPORTER_OTLP_ENDPOINT}"
OTEL_OTLP_INSTANCE_ID="${OTEL_OTLP_INSTANCE_ID}"
OTEL_OTLP_TOKEN="${OTEL_OTLP_TOKEN}"
OTEL_EXPORTER_OTLP_HEADERS="${OTEL_EXPORTER_OTLP_HEADERS}"

# ── Tunables ─────────────────────────────────────────────────────────────────
ANTHROPIC_CAP_USD_PER_DAY="${ANTHROPIC_CAP_USD_PER_DAY}"
NC_AI_ADOPTION_RATE="${NC_AI_ADOPTION_RATE}"
NC_TOTAL_USERS="${NC_TOTAL_USERS}"
SB_TOTAL_USERS="${SB_TOTAL_USERS}"

# ── Optional Ingress (leave blank to disable; chart defaults to off) ─────────
INGRESS_CLASS_NAME="${INGRESS_CLASS_NAME:-}"
INGRESS_NEONCART_HOST="${INGRESS_NEONCART_HOST:-}"
INGRESS_SUPPORTBOT_HOST="${INGRESS_SUPPORTBOT_HOST:-}"

# ── Cross-provider traffic split (empty = chart default anthropic:1,ollama:2) ─
PROVIDER_WEIGHTS="${PROVIDER_WEIGHTS:-}"

# ── Dashboard auto-import (both blank = skip; see next-steps for manual cmd) ──
GRAFANA_URL="${GRAFANA_URL:-}"
GRAFANA_API_TOKEN="${GRAFANA_API_TOKEN:-}"
EOF
  chmod 600 "$ENV_FILE"
  ok "Wrote ${ENV_FILE} (chmod 600)"
}

# ── Review gate ───────────────────────────────────────────────────────────────
review_env() {
  hdr "Review .env before continuing"
  say "Open ${ENV_FILE} in another terminal if you want to verify."
  say "(Secrets are not echoed here.)"
  say ""
  local resp
  read -r -p "Continue with these values? [Y/n]: " resp
  resp="${resp:-Y}"
  if [[ ! "$resp" =~ ^[Yy]$ ]]; then
    say "Aborting. Edit ${ENV_FILE} and rerun install.sh."
    exit 0
  fi
}

# ── Generate users.yaml ───────────────────────────────────────────────────────
generate_users() {
  hdr "Generating synthetic-user pool"
  local regen="${SCRIPT_DIR}/regenerate-users.py"
  if [[ ! -f "$regen" ]]; then
    die "Missing ${regen} — repo is incomplete."
  fi
  # --seed 42 by default; user can rerun the script manually with a different seed
  python3 "$regen" \
    --seed 42 \
    --nc-count "${NC_TOTAL_USERS}" \
    --sb-count "${SB_TOTAL_USERS}" \
    --out "${USERS_FILE}"
  ok "Wrote ${USERS_FILE}"
}

# ── Generate values-overrides YAML from .env ──────────────────────────────────
write_values_override() {
  hdr "Generating Helm values overrides"
  # All strings double-quoted so headers like Basic <base64> with '=' don't trip YAML.
  cat > "${VALUES_OVERRIDE}" <<EOF
# Auto-generated by tools/install.sh — DO NOT commit. Gitignored.
# Regenerate by re-running tools/install.sh.
global:
  anthropic:
    apiKey: "${CLAUDE_API_KEY}"
  openai:
    apiKey: "${OPENAI_API_KEY:-}"
  gemini:
    apiKey: "${GEMINI_API_KEY:-}"
  ollama:
    baseUrl: "${OLLAMA_BASE_URL:-}"
  sigil:
    endpoint: "${SIGIL_ENDPOINT}"
    protocol: "${SIGIL_PROTOCOL:-http}"
    authMode: "${SIGIL_AUTH_MODE:-basic}"
    tenantId: "${SIGIL_AUTH_TENANT_ID}"
    token: "${SIGIL_AUTH_TOKEN}"
  otel:
    endpoint: "${OTEL_EXPORTER_OTLP_ENDPOINT}"
    headers: "${OTEL_EXPORTER_OTLP_HEADERS}"
  grafana:
    url: "${GRAFANA_URL:-}"
  caps:
    anthropic:
      usdPerDay: ${ANTHROPIC_CAP_USD_PER_DAY:-30}
  # Only emit a modelWeights override when the user actually set an env var —
  # otherwise the chart's helm/values.yaml default (a weighted Claude pool)
  # would get clobbered with "" and the gateway would fall back to a single
  # default model. The block is built up conditionally below.
EOF
  # Append only the weight overrides the user actually set, so empty env vars
  # don't clobber the chart's bundled defaults.
  local _wrote_header=0
  _emit_weight() {
    local key="$1"
    local val="$2"
    if [[ -n "$val" ]]; then
      if [[ "$_wrote_header" -eq 0 ]]; then
        printf '  modelWeights:\n' >> "${VALUES_OVERRIDE}"
        _wrote_header=1
      fi
      printf '    %s: "%s"\n' "$key" "$val" >> "${VALUES_OVERRIDE}"
    fi
  }
  _emit_weight anthropic "${ANTHROPIC_MODEL_WEIGHTS:-}"
  _emit_weight openai    "${OPENAI_MODEL_WEIGHTS:-}"
  _emit_weight gemini    "${GEMINI_MODEL_WEIGHTS:-}"
  _emit_weight ollama    "${OLLAMA_MODEL_WEIGHTS:-}"

  # Optional cross-provider weighting (e.g. bias synthetic traffic toward Ollama).
  if [[ -n "${PROVIDER_WEIGHTS:-}" ]]; then
    printf '  providerWeights: "%s"\n' "${PROVIDER_WEIGHTS}" >> "${VALUES_OVERRIDE}"
  fi

  # Resume the heredoc-style append for the loadgen section.
  cat >> "${VALUES_OVERRIDE}" <<EOF
loadgen:
  ncTotalUsers: ${NC_TOTAL_USERS:-200}
  ncAiAdoptionRate: ${NC_AI_ADOPTION_RATE:-0.25}
  sbTotalUsers: ${SB_TOTAL_USERS:-30}
EOF

  # Optional Ingress — emit a full block only when at least one host is set.
  # The chart's default is ingress.enabled=false, so silence == no Ingress
  # objects. install.sh rewrites this file every run, so persisting the
  # values here (rather than hand-editing the override file) is the
  # reproducible path.
  if [[ -n "${INGRESS_NEONCART_HOST:-}" || -n "${INGRESS_SUPPORTBOT_HOST:-}" ]]; then
    cat >> "${VALUES_OVERRIDE}" <<EOF
ingress:
  enabled: true
  className: "${INGRESS_CLASS_NAME:-}"
  neoncart:
    host: "${INGRESS_NEONCART_HOST:-}"
  supportbot:
    host: "${INGRESS_SUPPORTBOT_HOST:-}"
EOF
  fi

  ok "Wrote ${VALUES_OVERRIDE}"
}

# ── Helm install ──────────────────────────────────────────────────────────────
helm_install() {
  hdr "Helm install"
  if [[ ! -f "${HELM_DIR}/Chart.yaml" ]]; then
    die "Helm chart not found at ${HELM_DIR}/Chart.yaml"
  fi
  if ! command -v helm >/dev/null 2>&1; then
    die "helm is not on PATH. Install from https://helm.sh/docs/intro/install/"
  fi

  say "Running helm upgrade --install ..."
  helm upgrade --install ai-o11y-demo-apps "${HELM_DIR}" \
    --values "${HELM_DIR}/values.yaml" \
    --values "${VALUES_OVERRIDE}" \
    --wait --timeout 10m
  ok "Helm install complete"
}

# ── Dashboard auto-import ─────────────────────────────────────────────────────
import_dashboards() {
  hdr "Dashboard import"
  if [[ -z "${GRAFANA_URL:-}" || -z "${GRAFANA_API_TOKEN:-}" ]]; then
    say "GRAFANA_URL or GRAFANA_API_TOKEN not set — skipping auto-import."
    say "Run it later with:"
    say "  GRAFANA_URL=https://your-stack.grafana.net \\"
    say "  GRAFANA_API_TOKEN=glsa_... \\"
    say "  bash ${REPO_ROOT}/dashboards/import.sh"
    DASHBOARDS_IMPORTED=0
    return 0
  fi
  if GRAFANA_URL="$GRAFANA_URL" GRAFANA_API_TOKEN="$GRAFANA_API_TOKEN" \
       bash "${REPO_ROOT}/dashboards/import.sh"; then
    ok "Dashboards imported"
    DASHBOARDS_IMPORTED=1
  else
    say "Dashboard import returned a non-zero status — see output above."
    DASHBOARDS_IMPORTED=0
  fi
}

# ── Next steps banner ─────────────────────────────────────────────────────────
print_next_steps() {
  hdr "Next steps"
  cat <<'EOF'
  1. Reach the NeonCart UI in a browser:
        kubectl port-forward svc/neoncart-web 8080:8000 -n neoncart
     then open http://localhost:8080

  2. Reach the SupportBot UI:
        kubectl port-forward svc/supportbot-web 8081:8000 -n support-bot

  3. Open the AI Observability dashboards in Grafana Cloud:
        Apps  ->  AI Observability  ->  Generations / Conversations / Agents

  4. Verify everything came up healthy:
        ./tools/verify.sh

  5. To regenerate the synthetic-user pool with a new seed:
        python3 tools/regenerate-users.py --seed 99
EOF
  if [[ "${DASHBOARDS_IMPORTED:-0}" -ne 1 ]]; then
    cat <<EOF

  6. Import the 4 use-case dashboards into your Grafana:
        GRAFANA_URL=https://your-stack.grafana.net \\
        GRAFANA_API_TOKEN=glsa_... \\
        bash ${REPO_ROOT}/dashboards/import.sh
EOF
  fi
}

# ── Main ──────────────────────────────────────────────────────────────────────
main() {
  hdr "ai-o11y-demo-apps installer"
  say "Repo root: ${REPO_ROOT}"

  check_prereqs
  load_existing_env
  collect_required
  collect_optional
  collect_tunables
  collect_ingress
  compute_otel_headers
  detect_alloy
  write_env_file
  review_env
  generate_users
  write_values_override
  helm_install
  import_dashboards
  print_next_steps

  hdr "Done"
}

main "$@"
