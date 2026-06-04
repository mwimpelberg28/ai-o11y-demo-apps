#!/usr/bin/env python3
"""Generates dashboards/problem-employees.json — a focused 5-panel dashboard
that surfaces the anomalies emitted by the supportbot-anomalies k6 scenario
(runaway loop + token glutton) so they're catchable at a glance.

Schema mirrors the v2 K8s-style format used by the other dashboards in
this repo. Re-run this script after editing it; the JSON output is the
canonical artifact and what import.sh ships.
"""
from __future__ import annotations
import json
from pathlib import Path

DS = {"name": "grafanacloud-prom"}
USER_RE = '.+@acme.com'
VERSION = "13.1.0-25668120414"


def prom_query(expr: str, *, legend: str | None = None, instant: bool = False, ref: str = "A") -> dict:
    spec = {"expr": expr}
    if legend:
        spec["legendFormat"] = legend
    if instant:
        spec["instant"] = True
    return {
        "kind": "PanelQuery",
        "spec": {
            "query": {
                "kind": "DataQuery",
                "group": "prometheus",
                "version": "v0",
                "datasource": DS,
                "spec": spec,
            },
            "refId": ref,
            "hidden": False,
        },
    }


def stat_panel(pid: int, title: str, description: str, expr: str, *,
               unit: str = "short", decimals: int = 0,
               color: str = "#ff6b00", text_mode: str = "value_and_name") -> dict:
    return {
        "kind": "Panel",
        "spec": {
            "id": pid,
            "title": title,
            "description": description,
            "links": [],
            "data": {
                "kind": "QueryGroup",
                "spec": {
                    "queries": [prom_query(expr, instant=True, legend="{{user_id}}")],
                    "transformations": [],
                    "queryOptions": {},
                },
            },
            "vizConfig": {
                "kind": "VizConfig",
                "group": "stat",
                "version": VERSION,
                "spec": {
                    "options": {
                        "colorMode": "background",
                        "graphMode": "area",
                        "justifyMode": "center",
                        "orientation": "auto",
                        "percentChangeColorMode": "standard",
                        "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                        "showPercentChange": False,
                        "textMode": text_mode,
                        "wideLayout": True,
                    },
                    "fieldConfig": {
                        "defaults": {
                            "unit": unit,
                            "decimals": decimals,
                            "thresholds": {"mode": "absolute", "steps": [
                                {"value": 0, "color": "green"},
                                {"value": 1000, "color": "orange"},
                                {"value": 10000, "color": "red"},
                            ]},
                            "color": {"mode": "fixed", "fixedColor": color},
                        },
                        "overrides": [],
                    },
                },
            },
        },
    }


def timeseries_panel(pid: int, title: str, description: str, expr: str, legend: str,
                     *, unit: str = "short", stacking: str = "none") -> dict:
    return {
        "kind": "Panel",
        "spec": {
            "id": pid,
            "title": title,
            "description": description,
            "links": [],
            "data": {
                "kind": "QueryGroup",
                "spec": {
                    "queries": [prom_query(expr, legend=legend)],
                    "transformations": [],
                    "queryOptions": {},
                },
            },
            "vizConfig": {
                "kind": "VizConfig",
                "group": "timeseries",
                "version": VERSION,
                "spec": {
                    "options": {
                        "legend": {
                            "calcs": ["mean", "max"],
                            "displayMode": "table",
                            "placement": "bottom",
                            "showLegend": True,
                            "sortBy": "Max",
                            "sortDesc": True,
                        },
                        "tooltip": {"hideZeros": False, "mode": "multi", "sort": "desc"},
                    },
                    "fieldConfig": {
                        "defaults": {
                            "unit": unit,
                            "decimals": 1,
                            "min": 0,
                            "thresholds": {"mode": "absolute", "steps": [
                                {"value": 0, "color": "green"},
                            ]},
                            "color": {"mode": "palette-classic"},
                            "custom": {
                                "axisBorderShow": False,
                                "axisCenteredZero": False,
                                "axisColorMode": "text",
                                "axisLabel": "",
                                "axisPlacement": "auto",
                                "barAlignment": 0,
                                "barWidthFactor": 0.9,
                                "drawStyle": "line",
                                "fillOpacity": 25,
                                "gradientMode": "opacity",
                                "hideFrom": {"legend": False, "tooltip": False, "viz": False},
                                "insertNulls": False,
                                "lineInterpolation": "smooth",
                                "lineWidth": 2,
                                "pointSize": 5,
                                "scaleDistribution": {"type": "linear"},
                                "showPoints": "auto",
                                "showValues": False,
                                "spanNulls": False,
                                "stacking": {"group": "A", "mode": stacking},
                                "thresholdsStyle": {"mode": "off"},
                            },
                        },
                        "overrides": [],
                    },
                },
            },
        },
    }


def grid_item(x, y, w, h, panel_name) -> dict:
    return {
        "kind": "GridLayoutItem",
        "spec": {
            "x": x, "y": y, "width": w, "height": h,
            "element": {"kind": "ElementReference", "name": panel_name},
        },
    }


def row(title: str, items: list[dict]) -> dict:
    return {
        "kind": "RowsLayoutRow",
        "spec": {
            "title": title,
            "collapse": False,
            "layout": {"kind": "GridLayout", "spec": {"items": items}},
        },
    }


# ── Build panels ──────────────────────────────────────────────────────────────

elements = {}

# Row 1 stats — current top offenders
elements["panel-1"] = stat_panel(
    1,
    "Top output-token user (5m)",
    "User with the highest output-token burn in the last 5 minutes. Catches the token-glutton anomaly (one user requests huge documents).",
    f'topk(1, sum by (user_id) (increase(gen_ai_user_tokens_total{{gen_ai_token_type="output",user_id=~"{USER_RE}"}}[5m])))',
    unit="short",
    decimals=0,
    color="#ff6b00",
    text_mode="value_and_name",
)

elements["panel-2"] = stat_panel(
    2,
    "Top call-rate user (5m)",
    "User firing the most LLM calls in the last 5 minutes (per minute). Catches the runaway-loop anomaly (one user stuck in a retry loop).",
    f'topk(1, sum by (user_id) (rate(gen_ai_user_tokens_total{{gen_ai_token_type="input",user_id=~"{USER_RE}"}}[5m])) * 60)',
    unit="cps",
    decimals=1,
    color="#dc267f",
    text_mode="value_and_name",
)

# Row 2 timeseries — per-user spikes
elements["panel-3"] = timeseries_panel(
    3,
    "Output tokens/min by user — top 5 (5m rate)",
    "Output-token rate per acme.com user. A user pegging high above the pack is a token-glutton spike.",
    f'topk(5, sum by (user_id) (rate(gen_ai_user_tokens_total{{gen_ai_token_type="output",user_id=~"{USER_RE}"}}[5m])) * 60)',
    legend="{{user_id}}",
    unit="short",
)

elements["panel-4"] = timeseries_panel(
    4,
    "Calls/min by user — top 5 (1m rate)",
    "LLM-call rate per acme.com user. A user pegging 10-50x the others is a runaway-loop spike — k6 anomaly scenario runs 180s bursts.",
    f'topk(5, sum by (user_id) (rate(gen_ai_user_tokens_total{{gen_ai_token_type="input",user_id=~"{USER_RE}"}}[1m])) * 60)',
    legend="{{user_id}}",
    unit="cpm",
)

# Row 3 timeseries — provider distribution
elements["panel-5"] = timeseries_panel(
    5,
    "acme.com traffic by provider (1m rate)",
    "All acme.com SB user traffic split by provider. Anomaly bursts pin preferred_provider=ollama so spikes shouldn't move the anthropic line.",
    f'sum by (gen_ai_system) (rate(gen_ai_user_tokens_total{{user_id=~"{USER_RE}"}}[1m]))',
    legend="{{gen_ai_system}}",
    unit="short",
    stacking="normal",
)

# ── Build layout ──────────────────────────────────────────────────────────────

layout = {
    "kind": "RowsLayout",
    "spec": {
        "rows": [
            row("🚨 Right now", [
                grid_item(0, 0, 12, 5, "panel-1"),
                grid_item(12, 0, 12, 5, "panel-2"),
            ]),
            row("📈 Per-user spikes (top 5 of acme.com)", [
                grid_item(0, 0, 12, 9, "panel-3"),
                grid_item(12, 0, 12, 9, "panel-4"),
            ]),
            row("🔍 Provider routing — confirms Ollama pinning held", [
                grid_item(0, 0, 24, 7, "panel-5"),
            ]),
        ],
    },
}

# ── Build dashboard ───────────────────────────────────────────────────────────

dashboard = {
    "apiVersion": "dashboard.grafana.app/v2",
    "kind": "Dashboard",
    "metadata": {
        "name": "ai-o11y-problem-employees",
        # namespace + stack-managed fields are injected by import.sh on push.
    },
    "spec": {
        "title": "AI o11y — Problem Employees",
        "description": "Per-user anomaly catch for SupportBot. Surfaces token-glutton bursts (10-50x output tokens for 2 min) and runaway-loop bursts (10-50 calls/min for 3 min) emitted by the supportbot-anomalies k6 scenario. Same panels catch real-world anomalies of the same shape.",
        "tags": ["ai-o11y-demo-apps", "anomalies", "supportbot"],
        "timeSettings": {
            "timezone": "browser",
            "from": "now-1h",
            "to": "now",
            "autoRefresh": "30s",
            "autoRefreshIntervals": ["5s", "10s", "30s", "1m", "5m", "15m", "30m", "1h"],
            "hideTimepicker": False,
            "fiscalYearStartMonth": 0,
        },
        "annotations": [],
        "cursorSync": "Off",
        "editable": True,
        "links": [],
        "liveNow": False,
        "preload": False,
        "variables": [],
        "elements": elements,
        "layout": layout,
    },
}

out = Path(__file__).resolve().parent.parent / "dashboards" / "problem-employees.json"
out.write_text(json.dumps(dashboard, indent=2) + "\n")
print(f"wrote {out} ({out.stat().st_size} bytes, {len(elements)} panels)")
