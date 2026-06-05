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


def table_panel(pid: int, title: str, description: str,
                queries: list[tuple[str, str]],
                join_field: str,
                column_order: list[tuple[str, str]],  # (raw_field, display_name)
                sort_by_display: str,
                sort_desc: bool = True,
                hidden_columns: list[str] | None = None,
                shared_label_fields: list[str] | None = None,
                column_links: dict[str, list[dict]] | None = None,
                join_mode: str = "outer") -> dict:
    """Build a v2 table panel that joins multiple instant queries on `join_field`.

    `queries` is a list of (refId, expr). Each query renders one Value #refId
    column. `column_order` is the desired column ordering by raw name; the
    first column should usually be the join_field.

    `shared_label_fields` lists labels that appear on EVERY query (e.g. user_id
    when each query groups by both session_id and user_id). After joinByField,
    those columns get suffixed " 1", " 2", ... — we keep " 1" renamed and drop
    the rest.

    `hidden_columns` is a list of (already-renamed) display names to hide via
    excludeByName. Useful for fields that exist only to feed a data link.

    `column_links` maps display name → list of {title, url, targetBlank} dicts.
    """
    panel_queries = []
    for ref, expr in queries:
        panel_queries.append({
            "kind": "PanelQuery",
            "spec": {
                "query": {
                    "kind": "DataQuery",
                    "group": "prometheus",
                    "version": "v0",
                    "datasource": DS,
                    "spec": {"expr": expr, "format": "table", "instant": True},
                },
                "refId": ref,
                "hidden": False,
            },
        })

    # column_order may reference raw label names that, after joinByField,
    # are suffixed with " 1" (the first query's copy). Build the rename map
    # accordingly: if the raw name is in shared_label_fields, look for
    # "<raw> 1" instead of "<raw>".
    shared = set(shared_label_fields or [])
    index_by_name = {}
    rename_by_name = {}
    for i, (raw, disp) in enumerate(column_order):
        key = f"{raw} 1" if raw in shared else raw
        index_by_name[key] = i
        rename_by_name[key] = disp

    # Hide every Time # column produced by `format: table`, plus extra
    # copies of shared-label columns (suffixes " 2", " 3", ...).
    exclude_by_name = {f"Time {i+1}": True for i in range(len(queries))}
    for raw in shared:
        for i in range(2, len(queries) + 1):
            exclude_by_name[f"{raw} {i}"] = True
    # excludeByName matches against the RAW field name (before rename), so
    # if the caller passes a display name in hidden_columns, look up the raw.
    disp_to_raw = {disp: raw for raw, disp in rename_by_name.items()}
    for hide in (hidden_columns or []):
        exclude_by_name[disp_to_raw.get(hide, hide)] = True

    overrides = []
    for raw, disp in column_order:
        if "Wasted" in disp:
            # Headline metric. color-background with thresholds (NOT continuous)
            # so each row reads at a glance: green = fine, red = bad — instead of
            # the whole column going solid red because everyone's "above the min".
            overrides.append({
                "matcher": {"id": "byName", "options": disp},
                "properties": [
                    {"id": "unit", "value": "currencyUSD"},
                    {"id": "decimals", "value": 2},
                    {"id": "min", "value": 0},
                    {"id": "custom.cellOptions",
                     "value": {"mode": "gradient", "type": "color-background"}},
                    {"id": "color", "value": {"mode": "thresholds"}},
                    {"id": "thresholds", "value": {"mode": "absolute", "steps": [
                        {"value": 0,     "color": "green"},
                        {"value": 5,     "color": "yellow"},
                        {"value": 25,    "color": "orange"},
                        {"value": 100,   "color": "red"},
                        {"value": 500,   "color": "dark-red"},
                    ]}},
                ],
            })
        elif "Cost" in disp or "$" in disp:
            overrides.append({
                "matcher": {"id": "byName", "options": disp},
                "properties": [
                    {"id": "unit", "value": "currencyUSD"},
                    {"id": "decimals", "value": 2},
                    {"id": "min", "value": 0},
                    {"id": "custom.cellOptions",
                     "value": {"mode": "gradient", "type": "color-background"}},
                    {"id": "color", "value": {"mode": "continuous-YlOrRd"}},
                ],
            })
        elif "Token" in disp:
            overrides.append({
                "matcher": {"id": "byName", "options": disp},
                "properties": [
                    {"id": "unit", "value": "short"},
                    {"id": "decimals", "value": 0},
                    {"id": "min", "value": 0},
                    {"id": "custom.cellOptions",
                     "value": {"mode": "gradient", "type": "gauge", "valueDisplayMode": "text"}},
                    {"id": "color", "value": {"mode": "continuous-blues"}},
                ],
            })
        elif disp in ("Conversation", "User"):
            props = [
                {"id": "custom.width", "value": 280 if disp == "Conversation" else 220},
                {"id": "custom.align", "value": "left"},
            ]
            if column_links and disp in column_links:
                props.append({"id": "links", "value": column_links[disp]})
            overrides.append({
                "matcher": {"id": "byName", "options": disp},
                "properties": props,
            })
        elif "Score" in disp:
            overrides.append({
                "matcher": {"id": "byName", "options": disp},
                "properties": [
                    {"id": "unit", "value": "percent"},
                    {"id": "decimals", "value": 0},
                    {"id": "min", "value": 0},
                    {"id": "max", "value": 100},
                    {"id": "custom.cellOptions",
                     "value": {"mode": "gradient", "type": "gauge", "valueDisplayMode": "text"}},
                    {"id": "thresholds", "value": {"mode": "absolute", "steps": [
                        {"value": 0,  "color": "red"},
                        {"value": 50, "color": "orange"},
                        {"value": 75, "color": "green"},
                    ]}},
                ],
            })

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
                    "queries": panel_queries,
                    "transformations": [
                        {
                            "kind": "Transformation",
                            "group": "joinByField",
                            "spec": {"options": {"byField": join_field, "mode": join_mode}},
                        },
                        {
                            "kind": "Transformation",
                            "group": "organize",
                            "spec": {
                                "options": {
                                    "excludeByName": exclude_by_name,
                                    "indexByName": index_by_name,
                                    "renameByName": rename_by_name,
                                },
                            },
                        },
                        {
                            "kind": "Transformation",
                            "group": "sortBy",
                            "spec": {
                                "options": {
                                    "fields": {},
                                    "sort": [{"desc": sort_desc, "field": sort_by_display}],
                                },
                            },
                        },
                    ],
                    "queryOptions": {},
                },
            },
            "vizConfig": {
                "kind": "VizConfig",
                "group": "table",
                "version": VERSION,
                "spec": {
                    "options": {
                        "cellHeight": "md",
                        "showHeader": True,
                        "sortBy": [{"desc": True, "displayName": sort_by_display}],
                    },
                    "fieldConfig": {
                        "defaults": {
                            "thresholds": {"mode": "absolute", "steps": [
                                {"value": 0, "color": "green"},
                            ]},
                            "custom": {
                                "align": "left",
                                "cellOptions": {"type": "auto"},
                                "footer": {"reducers": []},
                                "inspect": False,
                            },
                        },
                        "overrides": overrides,
                    },
                },
            },
        },
    }


def text_panel(pid: int, title: str, markdown: str) -> dict:
    """A Grafana text panel rendering markdown — used for inline explainers."""
    return {
        "kind": "Panel",
        "spec": {
            "id": pid,
            "title": title,
            "description": "",
            "links": [],
            "data": {"kind": "QueryGroup", "spec": {"queries": [], "transformations": [], "queryOptions": {}}},
            "vizConfig": {
                "kind": "VizConfig",
                "group": "text",
                "version": VERSION,
                "spec": {
                    "options": {"mode": "markdown", "content": markdown},
                    "fieldConfig": {"defaults": {}, "overrides": []},
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
    f'topk(1, sum by (user_id) (rate(gen_ai_user_calls_total{{user_id=~"{USER_RE}"}}[5m])) * 60)',
    unit="cpm",
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
    "Calls/min by user — top 5 (5m rate)",
    "LLM-call rate per acme.com user. A user pegging 10-50x the others is a runaway-loop spike — k6 anomaly scenario runs 180s bursts.",
    f'topk(5, sum by (user_id) (rate(gen_ai_user_calls_total{{user_id=~"{USER_RE}"}}[5m])) * 60)',
    legend="{{user_id}}",
    unit="cpm",
)

# Row 3 timeseries — provider distribution
elements["panel-5"] = timeseries_panel(
    5,
    "acme.com calls/min by provider (5m rate)",
    "All acme.com SB user traffic split by provider. Anomaly bursts pin preferred_provider=ollama so spikes shouldn't move the anthropic line.",
    f'sum by (gen_ai_system) (rate(gen_ai_user_calls_total{{user_id=~"{USER_RE}"}}[5m])) * 60',
    legend="{{gen_ai_system}}",
    unit="cpm",
    stacking="normal",
)

# Row 4 table — top conversations by spend
# Anomaly bursts tag their session_ids with `sess_anomrun_*` and
# `sess_anomglut_*` so they're identifiable as the "title" column. Regular
# SB sessions (sess_<hex>) still show up; they're just much cheaper and
# fall to the bottom of the cost-sorted table.
_FILTER = f'user_id=~"{USER_RE}",session_id!=""'
# Cost-fill + annualize. record_cost skips $0, so Ollama sessions have
# no cost series — `or (output_tokens * 0)` ensures every session has
# SOME cost value (real $$ for Anthropic, $0 for Ollama) so Ollama
# bursts survive the inner-join filter. The × 12 projects the 30d window
# up to an annual rate, which makes the numbers feel real in a demo
# ($0.97/month → $11.64/year) without changing what data we measure.
_ANNUALIZE = 12
_COST_FILLED_SESSION = (
    f'('
    f'sum by (session_id, user_id) (increase(gen_ai_client_cost_usd_total{{{_FILTER}}}[30d])) '
    f'or '
    f'sum by (session_id, user_id) (max_over_time(gen_ai_user_tokens_total{{{_FILTER},gen_ai_token_type="output"}}[30d])) * 0'
    f') * {_ANNUALIZE}'
)

elements["panel-6"] = table_panel(
    6,
    "Top problem conversations (30d)",
    "Top SupportBot conversations by $ wasted in the last 24h. Inner-joined: only conversations that have a cost figure AND have been scored by the evaluator appear (no blank cells). Click the Conversation cell to open the trace in AI o11y.",
    queries=[
        # user_id + conversation_id are present on every query so they need
        # `shared_label_fields` handling to dedupe after joinByField.
        ("input",  f'sum by (session_id, user_id, conversation_id) (increase(gen_ai_user_tokens_total{{{_FILTER},gen_ai_token_type="input"}}[30d]))'),
        ("output", f'sum by (session_id, user_id, conversation_id) (increase(gen_ai_user_tokens_total{{{_FILTER},gen_ai_token_type="output"}}[30d]))'),
        ("cost",   _COST_FILLED_SESSION),
        ("score",  f'max by (session_id, user_id) (conversation_eval_score{{user_id=~"{USER_RE}"}})'),
        # Per-session Wasted $$ = cost * (1 - score/100). Vector-multiplied
        # on (session_id, user_id). Ollama sessions get cost=0 from the fill
        # above so their Wasted $$ is also 0 — correctly attributed.
        ("waste",  (
            f'({_COST_FILLED_SESSION}) '
            f'* on(session_id, user_id) (1 - max by (session_id, user_id) (conversation_eval_score{{user_id=~"{USER_RE}"}}) / 100)'
        )),
    ],
    join_field="session_id",
    join_mode="inner",
    shared_label_fields=["user_id", "conversation_id"],
    column_order=[
        ("session_id",      "Conversation"),
        ("user_id",         "User"),
        ("Value #cost",     "$ Cost / yr"),
        ("Value #score",    "Eval Score"),
        ("Value #waste",    "Wasted $$ / yr"),
        ("Value #input",    "Input Tokens"),
        ("Value #output",   "Output Tokens"),
        ("conversation_id", "conv_id"),     # hidden — feeds the data link
    ],
    hidden_columns=["conv_id"],
    column_links={
        "Conversation": [{
            "title": "Open in AI o11y (Sigil)",
            "url": "https://stephenwagner.grafana.net/a/grafana-sigil-app/conversations/${__data.fields.conv_id}/explore?conversationTitle=${__data.fields.Conversation}",
            "targetBlank": True,
        }],
    },
    sort_by_display="Wasted $$ / yr",
)

# Row 5 table — worst employees (aggregate of conversations by user_id)
# Sorted by avg eval score ASC so the "least valuable AI use" employees
# bubble to the top — they're the demo's "who's wasting the budget" story.
_COST_FILLED_USER = (
    f'('
    f'sum by (user_id) (increase(gen_ai_client_cost_usd_total{{user_id=~"{USER_RE}"}}[30d])) '
    f'or '
    f'sum by (user_id) (max_over_time(gen_ai_user_tokens_total{{user_id=~"{USER_RE}",gen_ai_token_type="output"}}[30d])) * 0'
    f') * {_ANNUALIZE}'
)

elements["panel-7"] = table_panel(
    7,
    "Worst employees by waste (30d)",
    "Per-employee aggregate over the last 24h. Sessions = distinct conversation_ids the employee opened. Inner-joined: only employees with both a cost figure and at least one scored conversation appear.",
    queries=[
        ("sessions", f'count by (user_id) (count by (session_id, user_id) (max_over_time(gen_ai_user_tokens_total{{gen_ai_token_type="output",user_id=~"{USER_RE}"}}[30d])))'),
        ("input",    f'sum by (user_id) (increase(gen_ai_user_tokens_total{{gen_ai_token_type="input",user_id=~"{USER_RE}"}}[30d]))'),
        ("output",   f'sum by (user_id) (increase(gen_ai_user_tokens_total{{gen_ai_token_type="output",user_id=~"{USER_RE}"}}[30d]))'),
        ("cost",     _COST_FILLED_USER),
        ("score",    f'avg by (user_id) (conversation_eval_score{{user_id=~"{USER_RE}"}})'),
        # Waste = cost * (1 - score/100). user_id-only vector match.
        ("waste",    (
            f'({_COST_FILLED_USER}) '
            f'* on(user_id) (1 - avg by (user_id) (conversation_eval_score{{user_id=~"{USER_RE}"}}) / 100)'
        )),
    ],
    join_field="user_id",
    join_mode="inner",
    column_order=[
        ("user_id",         "User"),
        ("Value #cost",     "$ Cost / yr"),
        ("Value #score",    "Avg Eval Score"),
        ("Value #waste",    "Wasted $$ / yr"),
        ("Value #sessions", "Sessions"),
        ("Value #input",    "Input Tokens"),
        ("Value #output",   "Output Tokens"),
    ],
    sort_by_display="Wasted $$ / yr",
    sort_desc=True,  # most-wasteful first
)


# Explainer text panel that sits above both tables.
elements["panel-8"] = text_panel(
    8,
    "",
    (
        "### How to read these tables\n"
        "\n"
        "**Wasted $$ / yr** = `Cost × (1 − Eval Score / 100)`. It captures **both** "
        "axes of \"bad AI usage\" — spending money *and* not getting value for it.\n"
        "\n"
        "- Spend **$100/yr** with an eval score of **80%** → "
        "`$100 × (1 − 0.80)` = **$20 wasted**.\n"
        "- Spend **$100/yr** with an eval score of **0%** → "
        "`$100 × (1 − 0)` = **$100 wasted** — every dollar burned.\n"
        "- Score **100%** wastes **$0** no matter how much they spend — "
        "perfect use of the AI budget.\n"
        "\n"
        "**Why /yr?** The query measures the last 30 days then projects to an "
        "annual rate (× 12). At current usage patterns these are the yearly "
        "tabs. Eval scores come from an Ollama-driven judge (qwen2.5:14b) "
        "running outside the LLM gateway. Inner-joined — rows with blank "
        "cells are filtered out."
    ),
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
            row("💰 Wasted spend — how to read it", [
                grid_item(0, 0, 24, 6, "panel-8"),
            ]),
            row("🧾 Top problem conversations (30d)", [
                grid_item(0, 0, 24, 12, "panel-6"),
            ]),
            row("🧑 Worst employees (30d)", [
                grid_item(0, 0, 24, 10, "panel-7"),
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
