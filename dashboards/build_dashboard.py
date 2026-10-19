"""Build dashboards/observatory.json (Grafana) from the panel definitions below.

    python dashboards/build_dashboard.py

Metric panels read the CloudWatch namespace ColdStartObservatory (dimensions
workload/arch/packaging). Per-function and per-memory panels use Logs Insights
on the collector's log group, which holds one JSON line per invocation
(type=invocation) and per slow import (type=import).
"""
import json
from pathlib import Path

DS = {"type": "cloudwatch", "uid": "cloudwatch"}
COLLECTOR_LOGS = ["/aws/lambda/observatory-collector"]
_panels = []


def metric(metric_name, stat, dims, label):
    return {
        "refId": "A", "datasource": DS, "queryMode": "Metrics", "region": "default",
        "namespace": "ColdStartObservatory", "metricName": metric_name, "statistic": stat,
        "dimensions": dims, "matchExact": True, "period": "", "label": label,
        "metricQueryType": 0, "metricEditorMode": 0, "id": "", "expression": "",
    }


def logs(query, groups=COLLECTOR_LOGS):
    return {
        "refId": "A", "datasource": DS, "queryMode": "Logs", "region": "default",
        "logGroupNames": groups, "expression": query, "statsGroups": [],
    }


def panel(title, kind, target, x, y, w, h, description="", unit=None, options=None, overrides=None):
    p = {
        "id": len(_panels) + 1, "title": title, "type": kind, "description": description,
        "datasource": DS, "targets": [target],
        "gridPos": {"x": x, "y": y, "w": w, "h": h},
        "fieldConfig": {"defaults": {"unit": unit} if unit else {}, "overrides": overrides or []},
        "options": options or {},
    }
    _panels.append(p)


ALL = {"workload": "*", "arch": "*", "packaging": "*"}
COLD = 'filter type = "invocation" and ColdStart = 1'

# Row 1: headline numbers
panel("Cold starts", "stat", logs(f"{COLD} | stats count() as cold_starts"), 0, 0, 6, 4)
panel("Average init time", "stat", logs(f"{COLD} | stats avg(InitDurationMs) as avg_init"), 6, 0, 6, 4, unit="ms")
panel("Total cold-start cost", "stat", logs(f"{COLD} | stats sum(ColdStartCostUSD) as cost"), 12, 0, 6, 4,
      unit="currencyUSD", options={"reduceOptions": {"calcs": ["lastNotNull"]}, "text": {}, "graphMode": "none"},
      overrides=[{"matcher": {"id": "byName", "options": "cost"}, "properties": [{"id": "decimals", "value": 6}]}])
panel("Functions measured", "stat", logs(f"{COLD} | stats count_distinct(function_id) as functions"), 18, 0, 6, 4)

# Row 2: over time
panel("Cold starts over time", "timeseries",
      metric("ColdStart", "Sum", ALL, "${PROP('Dim.workload')} ${PROP('Dim.arch')} ${PROP('Dim.packaging')}"),
      0, 4, 12, 8, "Sum of the ColdStart metric (1 per cold invocation) per workload / arch / packaging.")
panel("Init duration over time", "timeseries",
      metric("InitDurationMs", "Average", ALL, "${PROP('Dim.workload')} ${PROP('Dim.arch')} ${PROP('Dim.packaging')}"),
      12, 4, 12, 8, "Average INIT (or SnapStart restore) duration.", unit="ms")

# Row 3: what drives init time
panel("Init time by memory and architecture", "table",
      logs(f"{COLD} | stats avg(InitDurationMs) as avg_ms, pct(InitDurationMs, 90) as p90_ms, count() as n"
           " by workload, memory_mb, arch | sort workload asc, memory_mb asc, arch asc"),
      0, 12, 12, 10, "More memory means more CPU during INIT. Compare x86 and arm64 at each size.", unit="ms",
      overrides=[{"matcher": {"id": "byName", "options": "n"}, "properties": [{"id": "unit", "value": "none"}]},
                 {"matcher": {"id": "byName", "options": "memory_mb"}, "properties": [{"id": "unit", "value": "decmbytes"}]}])
panel("Init time by packaging (data workload)", "bargauge",
      metric("InitDurationMs", "Average", {"workload": "data", "arch": "*", "packaging": "*"},
             "${PROP('Dim.packaging')} ${PROP('Dim.arch')}"),
      12, 12, 12, 10, "ZIP vs layer vs container image, for the same pandas workload.", unit="ms",
      options={"reduceOptions": {"calcs": ["mean"]}, "orientation": "horizontal", "displayMode": "gradient"})

# Row 4: cost and imports
panel("Cold-start cost per function", "table",
      logs(f"{COLD} | stats sum(ColdStartCostUSD) as cost_usd, count() as cold_starts, avg(InitDurationMs) as avg_init_ms"
           " by function_id | sort cost_usd desc"),
      0, 22, 12, 10, "INIT is billed like normal duration (since Aug 2025).",
      overrides=[{"matcher": {"id": "byName", "options": "cost_usd"}, "properties": [{"id": "decimals", "value": 7}]},
                 {"matcher": {"id": "byName", "options": "avg_init_ms"}, "properties": [{"id": "unit", "value": "ms"}]}])
panel("Slowest top-level imports", "table",
      logs('filter type = "import" and module not like /\\./ and module not like /^_/'
           " | stats avg(cumulative_us) / 1000 as avg_ms, count() as seen by function_id, module"
           " | sort avg_ms desc | limit 20"),
      12, 22, 12, 10, "Cumulative import time per top-level package (includes its submodules).",
      overrides=[{"matcher": {"id": "byName", "options": "avg_ms"}, "properties": [{"id": "unit", "value": "ms"}, {"id": "decimals", "value": 1}]}])

# Row 5: strategy experiment + recommendations (filled by the ML stage)
panel("SnapStart restore vs on-demand init", "table",
      logs('filter @message like /^REPORT/ and @message like /(Init|Restore) Duration/'
           ' | parse @message /(?<phase>Init|Restore) Duration: (?<ms>[\\d.]+) ms/'
           " | stats avg(ms) as avg_ms, pct(ms, 90) as p90_ms, count() as n by @log, phase"
           " | sort @log asc, phase asc",
           ["/aws/lambda/web-512-x86-zip", "/aws/lambda/data-512-x86-zip"]),
      0, 32, 12, 8, "Same functions: on-demand INIT vs SnapStart RESTORE, read from the functions' REPORT lines.", unit="ms",
      overrides=[{"matcher": {"id": "byName", "options": "n"}, "properties": [{"id": "unit", "value": "none"}]}])
panel("Strategy recommendations", "table",
      logs('filter type = "recommendation"'
           " | stats latest(strategy) as strategy, latest(monthly_cost_usd) as monthly_usd,"
           " latest(p95_ms) as p95_ms, latest(latency_target_ms) as target_ms,"
           " latest(pred_init_p50_ms) as init_p50_ms, latest(pred_init_p90_ms) as init_p90_ms"
           " by function_id | sort function_id asc", ["/observatory/recommendations"]),
      12, 32, 12, 8, "From ml/recommender.py via scripts/publish_recommendations.py: the cheapest strategy meeting the latency target.",
      overrides=[{"matcher": {"id": "byName", "options": "monthly_usd"}, "properties": [{"id": "unit", "value": "currencyUSD"}, {"id": "decimals", "value": 4}]}])

dashboard = {
    "uid": "cold-start-observatory",
    "title": "Cold-Start Observatory",
    "tags": ["lambda", "cold-start"],
    "timezone": "browser",
    "schemaVersion": 39,
    "time": {"from": "now-7d", "to": "now"},
    "refresh": "1m",
    "panels": _panels,
}

out = Path(__file__).with_name("observatory.json")
out.write_text(json.dumps(dashboard, indent=2))
print(f"Wrote {out} ({len(_panels)} panels)")
