"""Loadgen orchestrator.

A small FastAPI service that supervises K6 subprocesses against NeonCart
and SupportBot. It does NOT call LLM endpoints itself — K6 does. The
orchestrator's job is:

1. Read `config/users.yaml` at startup — the pool of synthetic users with
   stable cohort assignments (regenerate via `tools/regenerate-users.py`).
2. Spawn K6 subprocesses for each scenario (one per NC cohort + one SB).
   Each K6 process is fed the relevant slice of the user pool as a JSON
   file via `--env USERS_FILE=/path`. All scenarios run 24/7 regardless
   of provider cap state — the gateway routes individual requests to
   Ollama when Anthropic is capped (see `gateway/app/router.py`), so
   synthetic AI traffic stays steady on whichever provider is open.
3. Poll the LLM gateway's `/open` endpoint every few seconds and log
   Anthropic transitions for observability. The poller no longer tears
   down scenarios — interactive (real user) traffic always gets Claude
   ungated, and synthetic traffic rides the gateway's provider routing.
4. Expose `/health` (k8s liveness). Counter metrics ride the OTLP push
   pipeline that opentelemetry-instrument sets up — LLM/Sigil metrics
   still come from gateway/specialists.

Env vars (all documented in docs/LOADGEN.md):
    NC_TOTAL_USERS               (informational; truth comes from users.yaml)
    NC_AI_ADOPTION_RATE          (informational)
    NC_SESSIONS_PER_HOUR         (passed to K6 as $NC_SESSIONS_PER_HOUR)
    SB_TOTAL_USERS               (informational)
    SB_SESSIONS_PER_HOUR         (passed to K6)
    LOADGEN_TIME_OF_DAY          true/false; passed to K6
    LOADGEN_TZ                   passed to K6
    LOADGEN_POLL_OPEN_INTERVAL_SEC   default 5
    LOADGEN_NC_BASE_URL          default http://nc-web.neoncart.svc.cluster.local
    LOADGEN_SB_BASE_URL          default http://sb-web.support-bot.svc.cluster.local
    SB_USER_DOMAIN               default acme.com
    USERS_CONFIG_PATH            default /etc/loadgen/users.yaml
    GATEWAY_URL                  default http://llm-gateway.llm-gateway.svc.cluster.local
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import sys
import tempfile
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator

import httpx
import yaml
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from opentelemetry import metrics
from opentelemetry.metrics import Observation

log = logging.getLogger("loadgen.orchestrator")
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)


# ── Configuration ─────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Settings:
    gateway_url: str
    poll_interval_sec: float
    nc_base_url: str
    sb_base_url: str
    sb_user_domain: str
    users_config_path: str
    nc_sessions_per_hour: int
    sb_sessions_per_hour: int
    time_of_day: bool
    tz: str
    k6_binary: str
    scripts_dir: Path
    metrics_port: int

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            gateway_url=os.getenv(
                "GATEWAY_URL",
                "http://llm-gateway.llm-gateway.svc.cluster.local",
            ).rstrip("/"),
            poll_interval_sec=float(os.getenv("LOADGEN_POLL_OPEN_INTERVAL_SEC", "5")),
            nc_base_url=os.getenv(
                "LOADGEN_NC_BASE_URL",
                "http://nc-web.neoncart.svc.cluster.local",
            ).rstrip("/"),
            sb_base_url=os.getenv(
                "LOADGEN_SB_BASE_URL",
                "http://sb-web.support-bot.svc.cluster.local",
            ).rstrip("/"),
            sb_user_domain=os.getenv("SB_USER_DOMAIN", "acme.com"),
            users_config_path=os.getenv("USERS_CONFIG_PATH", "/etc/loadgen/users.yaml"),
            nc_sessions_per_hour=int(os.getenv("NC_SESSIONS_PER_HOUR", "60")),
            sb_sessions_per_hour=int(os.getenv("SB_SESSIONS_PER_HOUR", "30")),
            time_of_day=os.getenv("LOADGEN_TIME_OF_DAY", "false").lower() in ("1", "true", "yes"),
            tz=os.getenv("LOADGEN_TZ", "America/New_York"),
            k6_binary=os.getenv("K6_BINARY", "k6"),
            scripts_dir=Path(os.getenv("LOADGEN_SCRIPTS_DIR", "/loadgen/k6/scripts")),
            metrics_port=int(os.getenv("PORT", "8080")),
        )


# ── User pool ─────────────────────────────────────────────────────────────────

@dataclass
class UserPool:
    nc_non_ai: list[dict[str, Any]] = field(default_factory=list)
    nc_gift_finder: list[dict[str, Any]] = field(default_factory=list)
    nc_chatbot: list[dict[str, Any]] = field(default_factory=list)
    nc_both: list[dict[str, Any]] = field(default_factory=list)
    sb: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def load(cls, path: str) -> "UserPool":
        p = Path(path)
        if not p.exists():
            log.warning("users.yaml not found at %s; running with empty pool", path)
            return cls()
        data = yaml.safe_load(p.read_text())
        if not isinstance(data, dict):
            raise ValueError(f"{path}: expected a dict at top level")

        pool = cls()
        for u in data.get("nc_users", []):
            cohort = (u.get("cohort") or "").strip()
            if cohort == "non_ai":
                pool.nc_non_ai.append(u)
            elif cohort == "gift_finder":
                pool.nc_gift_finder.append(u)
            elif cohort == "chatbot":
                pool.nc_chatbot.append(u)
            elif cohort == "both":
                pool.nc_both.append(u)
            else:
                log.warning("nc user %s has unknown cohort %r; skipping", u.get("email"), cohort)
        for u in data.get("sb_users", []):
            pool.sb.append(u)

        log.info(
            "loaded users: nc_non_ai=%d nc_gift_finder=%d nc_chatbot=%d nc_both=%d sb=%d",
            len(pool.nc_non_ai),
            len(pool.nc_gift_finder),
            len(pool.nc_chatbot),
            len(pool.nc_both),
            len(pool.sb),
        )
        return pool


# ── Application metrics (orchestrator-internal) ───────────────────────────────
#
# These ride the OTLP push pipeline that opentelemetry-instrument sets up.
# There is no in-cluster Prometheus scraping this pod, so a /metrics endpoint
# would be invisible. Metric names + label keys are kept identical to the
# previous prometheus_client definitions so existing dashboards keep working.

_meter = metrics.get_meter("loadgen.orchestrator")

# Observable-gauge state ──────────────────────────────────────────────────────
# OTel observable gauges read their value via callback at collection time.
# We keep simple dicts that the supervisor/poller update, and the callbacks
# yield Observations over them.
_anthropic_open_state: int = 0
_k6_running_state: dict[str, int] = {}


def _set_anthropic_open(value: int) -> None:
    global _anthropic_open_state
    _anthropic_open_state = value


def _set_k6_running(scenario: str, value: int) -> None:
    _k6_running_state[scenario] = value


# NOTE: previously loadgen_gateway_anthropic_open + loadgen_k6_processes_running
# were observable gauges. The OTel Python SDK attaches an Exemplar struct to
# observable-gauge data points even with OTEL_METRICS_EXEMPLAR_FILTER=always_off
# (a long-standing quirk in opentelemetry-python). The exemplar's span_id/trace_id
# are None, the OTLP/HTTP encoder throws EncodingException on it, and the entire
# metric batch is dropped — so we'd lose every loadgen metric.
#
# Removed for now. The same information is recoverable from upstream sources:
#   - Anthropic-open state → query /open from the gateway or use cap metrics
#   - K6 process count    → derivable from loadgen_k6_restarts_total + spawn logs
# Track state via counters instead if needed later.

# Counters ────────────────────────────────────────────────────────────────────
_c_gateway_poll_failures = _meter.create_counter(
    "loadgen_gateway_poll_failures_total",
    description="Failures polling the LLM gateway /open endpoint.",
)
_c_k6_restarts = _meter.create_counter(
    "loadgen_k6_restarts_total",
    description="Number of times a K6 scenario was (re)started.",
)
_c_k6_terminations = _meter.create_counter(
    "loadgen_k6_terminations_total",
    description="Number of times a K6 scenario was terminated (shutdown or graceful stop).",
)


class _CounterShim:
    """Tiny shim providing the prometheus_client .labels().inc() surface."""

    def __init__(self, counter, label_keys: tuple[str, ...] = ()):
        self._counter = counter
        self._label_keys = label_keys

    def labels(self, **kwargs: str) -> "_BoundCounter":
        return _BoundCounter(self._counter, kwargs)

    def inc(self, value: float = 1) -> None:
        # No-label fast path
        self._counter.add(value)


class _BoundCounter:
    __slots__ = ("_counter", "_attrs")

    def __init__(self, counter, attrs: dict[str, str]):
        self._counter = counter
        self._attrs = attrs

    def inc(self, value: float = 1) -> None:
        self._counter.add(value, attributes=self._attrs)


m_gateway_poll_failures = _CounterShim(_c_gateway_poll_failures)
m_k6_restarts = _CounterShim(_c_k6_restarts, ("scenario",))
m_k6_terminations = _CounterShim(_c_k6_terminations, ("scenario",))


# ── K6 process management ─────────────────────────────────────────────────────

@dataclass
class Scenario:
    name: str
    script_filename: str
    base_url_env: str
    base_url: str
    users_payload: list[dict[str, Any]]
    sessions_per_hour: int


class K6Supervisor:
    """Manages one K6 subprocess per Scenario."""

    # Backoff schedule for restart-on-crash. Reset to 0 once a scenario
    # accumulates this much uptime in a single run (a healthy lifetime).
    _HEALTHY_UPTIME_SEC = 60.0
    _MAX_BACKOFF_SEC = 60.0

    def __init__(self, settings: Settings):
        self.settings = settings
        self._procs: dict[str, asyncio.subprocess.Process] = {}
        self._users_files: dict[str, str] = {}
        self._scenarios: dict[str, Scenario] = {}
        self._lock = asyncio.Lock()
        self._shutdown: bool = False
        self._start_times: dict[str, float] = {}
        self._restart_failures: dict[str, int] = {}

    def register(self, scenario: Scenario) -> None:
        self._scenarios[scenario.name] = scenario
        _set_k6_running(scenario.name, 0)

    async def start(self, scenario_name: str) -> None:
        """Spawn the K6 subprocess for `scenario_name` if not already running."""
        async with self._lock:
            if scenario_name in self._procs and self._procs[scenario_name].returncode is None:
                return  # already running

            sc = self._scenarios.get(scenario_name)
            if sc is None:
                log.error("unknown scenario %r", scenario_name)
                return

            if not sc.users_payload:
                log.warning("scenario %s has no users assigned; skipping spawn", scenario_name)
                return

            script_path = self.settings.scripts_dir / sc.script_filename
            if not script_path.exists():
                log.error("k6 script missing: %s", script_path)
                return

            # Write users JSON to a temp file for this run
            fd, tmp_path = tempfile.mkstemp(prefix=f"users-{scenario_name}-", suffix=".json")
            with os.fdopen(fd, "w") as f:
                json.dump(sc.users_payload, f)
            self._users_files[scenario_name] = tmp_path

            env = os.environ.copy()
            env["USERS_FILE"] = tmp_path
            env[sc.base_url_env] = sc.base_url
            env["SESSIONS_PER_HOUR"] = str(sc.sessions_per_hour)
            env["LOADGEN_TIME_OF_DAY"] = "true" if self.settings.time_of_day else "false"
            env["LOADGEN_TZ"] = self.settings.tz
            env["SB_USER_DOMAIN"] = self.settings.sb_user_domain
            env["SCENARIO_NAME"] = scenario_name

            # We rely on each script declaring its own scenarios + executor.
            # `--no-summary --no-thresholds` keeps stdout quiet; K6 still
            # writes its own metrics to its outputs (or to nothing if not
            # configured — fine for the demo, app-level telemetry is what we
            # care about).
            cmd = [
                self.settings.k6_binary,
                "run",
                "--quiet",
                "--no-summary",
                "--no-thresholds",
                # Distinct REST API port per scenario, or 0 to auto-pick — avoids
                # parallel-bind collisions (the 6565 default would race).
                "--address", "0.0.0.0:0",
                str(script_path),
            ]
            log.info("spawning k6 for scenario=%s cmd=%s", scenario_name, " ".join(cmd))
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            self._procs[scenario_name] = proc
            self._start_times[scenario_name] = time.monotonic()
            _set_k6_running(scenario_name, 1)
            m_k6_restarts.labels(scenario=scenario_name).inc()

            # Drain stdout in a background task — keeps the pipe from filling.
            asyncio.create_task(self._drain(scenario_name, proc))

    async def stop(self, scenario_name: str, reason: str = "manual") -> None:
        """SIGTERM the K6 process; it finishes its current iteration then exits."""
        async with self._lock:
            proc = self._procs.get(scenario_name)
            if proc is None or proc.returncode is not None:
                return
            log.info("stopping k6 scenario=%s reason=%s pid=%d", scenario_name, reason, proc.pid)
            try:
                proc.send_signal(signal.SIGTERM)
            except ProcessLookupError:
                pass
            m_k6_terminations.labels(scenario=scenario_name).inc()

        # Wait up to 30s for graceful shutdown; if it doesn't drain, SIGKILL.
        try:
            await asyncio.wait_for(proc.wait(), timeout=30)
        except asyncio.TimeoutError:
            log.warning("k6 scenario=%s did not exit after SIGTERM; sending SIGKILL", scenario_name)
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            await proc.wait()

    async def stop_all(self) -> None:
        self._shutdown = True
        await asyncio.gather(
            *(self.stop(name, reason="shutdown") for name in list(self._procs.keys())),
            return_exceptions=True,
        )

    def is_running(self, scenario_name: str) -> bool:
        proc = self._procs.get(scenario_name)
        return proc is not None and proc.returncode is None

    async def _drain(self, scenario_name: str, proc: asyncio.subprocess.Process) -> None:
        assert proc.stdout is not None
        try:
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                # Forward k6 output with a prefix so logs are scannable.
                sys.stdout.write(f"[k6:{scenario_name}] {line.decode(errors='replace')}")
                sys.stdout.flush()
        finally:
            rc = await proc.wait()
            uptime = time.monotonic() - self._start_times.get(scenario_name, time.monotonic())
            _set_k6_running(scenario_name, 0)
            log.info("k6 scenario=%s exited rc=%d uptime=%.1fs", scenario_name, rc, uptime)
            # Clean up users file
            path = self._users_files.pop(scenario_name, None)
            if path and os.path.exists(path):
                try:
                    os.unlink(path)
                except OSError:
                    pass

        # Auto-restart unless the supervisor is shutting down.
        if self._shutdown:
            return
        if uptime >= self._HEALTHY_UPTIME_SEC:
            self._restart_failures[scenario_name] = 0
        failures = self._restart_failures.get(scenario_name, 0)
        delay = min(2.0 ** failures, self._MAX_BACKOFF_SEC)
        self._restart_failures[scenario_name] = failures + 1
        log.warning(
            "k6 scenario=%s exited unexpectedly; restarting in %.1fs (consecutive failures=%d)",
            scenario_name, delay, failures + 1,
        )
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return
        if not self._shutdown:
            await self.start(scenario_name)


# ── Gateway poller ────────────────────────────────────────────────────────────

class GatewayPoller:
    """Polls the LLM gateway /open endpoint and tracks Anthropic state."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self._anthropic_open: bool | None = None  # None until first successful poll
        self._stop = asyncio.Event()

    @property
    def anthropic_open(self) -> bool:
        # If we have never had a successful poll, conservatively treat as
        # open — without the gateway we still want NC non-AI traffic and we
        # don't want to silently freeze on misconfiguration.
        return self._anthropic_open is not False

    async def run(self) -> None:
        """Background loop. Logs Anthropic open/close transitions for visibility."""
        url = f"{self.settings.gateway_url}/open"
        log.info("polling gateway at %s every %.1fs", url, self.settings.poll_interval_sec)

        async with httpx.AsyncClient(timeout=5.0) as client:
            while not self._stop.is_set():
                try:
                    resp = await client.get(url)
                    resp.raise_for_status()
                    body = resp.json()
                    providers = body.get("providers", {}) if isinstance(body, dict) else {}
                    ant = providers.get("anthropic") or {}
                    new_open = bool(ant.get("open", False)) if ant else False
                    _set_anthropic_open(1 if new_open else 0)
                    if new_open != self._anthropic_open:
                        log.info(
                            "gateway anthropic state changed: %s -> %s (reason=%s)",
                            self._anthropic_open,
                            new_open,
                            ant.get("reason"),
                        )
                        self._anthropic_open = new_open
                except Exception as e:
                    m_gateway_poll_failures.inc()
                    log.warning("gateway poll failed: %s", e)

                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=self.settings.poll_interval_sec)
                except asyncio.TimeoutError:
                    pass

    def stop(self) -> None:
        self._stop.set()


# ── Orchestrator ──────────────────────────────────────────────────────────────

# Scenario identifiers
SC_NC_NON_AI = "neoncart-non-ai"
SC_NC_GIFT = "neoncart-gift-finder"
SC_NC_CHAT = "neoncart-chatbot"
SC_NC_BOTH = "neoncart-both"
SC_SB = "supportbot"
SC_SB_ANOMALIES = "supportbot-anomalies"

# Number of fixed SB users that anomaly scenarios rotate through. Stable
# subset → "repeat offender" stories work for demos.
ANOMALY_USERS_PER_POOL = 5


class Orchestrator:
    def __init__(self, settings: Settings, pool: UserPool):
        self.settings = settings
        self.pool = pool
        self.supervisor = K6Supervisor(settings)
        self.poller = GatewayPoller(settings)
        self._poll_task: asyncio.Task | None = None
        self._register_scenarios()

    def _register_scenarios(self) -> None:
        self.supervisor.register(Scenario(
            name=SC_NC_NON_AI,
            script_filename="neoncart-non-ai.js",
            base_url_env="NC_BASE_URL",
            base_url=self.settings.nc_base_url,
            users_payload=self.pool.nc_non_ai,
            sessions_per_hour=self._scale(self.settings.nc_sessions_per_hour, 0.75),
        ))
        self.supervisor.register(Scenario(
            name=SC_NC_GIFT,
            script_filename="neoncart-gift-finder.js",
            base_url_env="NC_BASE_URL",
            base_url=self.settings.nc_base_url,
            users_payload=self.pool.nc_gift_finder,
            sessions_per_hour=self._scale(self.settings.nc_sessions_per_hour, 0.15),
        ))
        self.supervisor.register(Scenario(
            name=SC_NC_CHAT,
            script_filename="neoncart-chatbot.js",
            base_url_env="NC_BASE_URL",
            base_url=self.settings.nc_base_url,
            users_payload=self.pool.nc_chatbot,
            sessions_per_hour=self._scale(self.settings.nc_sessions_per_hour, 0.075),
        ))
        # Both — reuses the chatbot script (the script picks gift-finder OR
        # chatbot per iteration); approximated for the demo.
        self.supervisor.register(Scenario(
            name=SC_NC_BOTH,
            script_filename="neoncart-chatbot.js",
            base_url_env="NC_BASE_URL",
            base_url=self.settings.nc_base_url,
            users_payload=self.pool.nc_both,
            sessions_per_hour=self._scale(self.settings.nc_sessions_per_hour, 0.025),
        ))
        self.supervisor.register(Scenario(
            name=SC_SB,
            script_filename="supportbot.js",
            base_url_env="SB_BASE_URL",
            base_url=self.settings.sb_base_url,
            users_payload=self.pool.sb,
            sessions_per_hour=self.settings.sb_sessions_per_hour,
        ))
        # SB anomalies — one "problem" run every 10 min against a fixed
        # subset of 5 employees. The script self-paces; sessions_per_hour
        # is informational only here.
        self.supervisor.register(Scenario(
            name=SC_SB_ANOMALIES,
            script_filename="supportbot-anomalies.js",
            base_url_env="SB_BASE_URL",
            base_url=self.settings.sb_base_url,
            users_payload=self.pool.sb[:ANOMALY_USERS_PER_POOL],
            sessions_per_hour=6,
        ))

    @staticmethod
    def _scale(rate: int, fraction: float) -> int:
        # Floor at 1/hr so a small cohort still gets occasional traffic.
        return max(1, int(round(rate * fraction)))

    async def start(self) -> None:
        # All scenarios run 24/7 — the gateway routes individual requests
        # to whichever provider is open, so loadgen doesn't need to gate.
        for name in self.supervisor._scenarios:
            await self.supervisor.start(name)

        self._poll_task = asyncio.create_task(self.poller.run())

    async def stop(self) -> None:
        if self._poll_task:
            self.poller.stop()
            try:
                await asyncio.wait_for(self._poll_task, timeout=10)
            except asyncio.TimeoutError:
                self._poll_task.cancel()
        await self.supervisor.stop_all()

    def status(self) -> dict[str, Any]:
        return {
            "anthropic_open": self.poller.anthropic_open,
            "scenarios": {
                name: {
                    "running": self.supervisor.is_running(name),
                    "user_count": len(self.supervisor._scenarios[name].users_payload),
                }
                for name in self.supervisor._scenarios
            },
        }


# ── FastAPI app ───────────────────────────────────────────────────────────────

_orch: Orchestrator | None = None


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    global _orch
    settings = Settings.from_env()
    pool = UserPool.load(settings.users_config_path)
    _orch = Orchestrator(settings, pool)
    await _orch.start()
    log.info("orchestrator started")
    try:
        yield
    finally:
        log.info("orchestrator shutting down")
        if _orch is not None:
            await _orch.stop()


app = FastAPI(title="loadgen-orchestrator", version=os.getenv("APP_VERSION", "0.1.0"), lifespan=lifespan)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/readyz")
def readyz() -> JSONResponse:
    if _orch is None:
        return JSONResponse({"status": "starting"}, status_code=503)
    return JSONResponse({"status": "ready", **_orch.status()})


@app.get("/status")
def status() -> dict[str, Any]:
    if _orch is None:
        return {"status": "starting"}
    return _orch.status()


# No /metrics endpoint: custom metrics now ride the OTLP push pipeline that
# opentelemetry-instrument sets up. There is no Prometheus scrape configured
# for this pod.
