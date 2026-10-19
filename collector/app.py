"""Collector Lambda: CloudWatch Logs subscription -> DynamoDB rows + EMF metrics.

Receives REPORT and `import time:` lines from the test functions' log groups
(filter pattern `?REPORT ?"import time"`). Payload is base64 + gzip JSON.
"""
import base64
import gzip
import json
import os
import re
from datetime import datetime, timezone
from decimal import Decimal

import boto3

from cost import init_cost_usd, restore_cost_usd

# Keep import-profile rows only for modules that take at least this long
# (cumulative). Full profiles are ~1000 modules per cold start for pandas,
# which would cost more in DynamoDB writes than the rest of the project.
MIN_IMPORT_US = int(os.environ.get("MIN_IMPORT_US", "1000"))

FUNCTION_ID = re.compile(r"^(tiny|web|data|ml)-(\d+)-(x86|arm64)-(zip|layer|image)$")
REPORT_FIELDS = {
    "Duration": "duration_ms",
    "Billed Duration": "billed_ms",
    "Memory Size": "memory_mb",
    "Max Memory Used": "max_mem_mb",
    "Init Duration": "init_ms",
    "Restore Duration": "restore_ms",
    "Billed Restore Duration": "billed_restore_ms",
}
REPORT_FIELD = re.compile(
    r"(Billed Restore Duration|Billed Duration|Init Duration|Restore Duration|Duration|Memory Size|Max Memory Used): ([\d.]+)"
)
REPORT_ID = re.compile(r"REPORT RequestId: (\S+)")
IMPORT_LINE = re.compile(r"import time:\s+(\d+)\s*\|\s*(\d+)\s*\|\s*(\S+)")

_tables = {}


def _table(env_name):
    if env_name not in _tables:
        _tables[env_name] = boto3.resource("dynamodb").Table(os.environ[env_name])
    return _tables[env_name]


def decode(event):
    return json.loads(gzip.decompress(base64.b64decode(event["awslogs"]["data"])))


def parse_function_id(function_id):
    m = FUNCTION_ID.match(function_id)
    if not m:
        return None
    workload, memory, arch, packaging = m.groups()
    return {"workload": workload, "memory_mb": int(memory), "arch": arch, "packaging": packaging}


def parse_report(message):
    m = REPORT_ID.search(message)
    if not m:
        return None
    row = {"request_id": m.group(1)}
    for name, value in REPORT_FIELD.findall(message):
        row[REPORT_FIELDS[name]] = float(value)
    row["is_cold"] = "init_ms" in row or "restore_ms" in row
    return row


def parse_import(message):
    m = IMPORT_LINE.search(message)
    if not m:
        return None
    return {"self_us": int(m.group(1)), "cumulative_us": int(m.group(2)), "module": m.group(3)}


def iso(ms):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def build_rows(payload):
    """Pure parsing step: returns (invocation rows, import-profile rows)."""
    function_id = payload["logGroup"].rsplit("/", 1)[-1]
    meta = parse_function_id(function_id)
    if meta is None:
        return [], []

    imports, reports = [], []
    for ev in payload["logEvents"]:
        msg = ev["message"]
        if "import time:" in msg:
            imp = parse_import(msg)
            if imp:
                imp["ts"] = iso(ev["timestamp"])
                imports.append(imp)
        elif msg.startswith("REPORT"):
            rep = parse_report(msg)
            if rep:
                rep["ts"] = iso(ev["timestamp"])
                reports.append(rep)

    invocations = []
    for rep in reports:
        row = {"function_id": function_id, **meta, **rep}
        if "init_ms" in row:
            row["init_cost_usd"] = init_cost_usd(meta["memory_mb"], meta["arch"], row["init_ms"])
        if "restore_ms" in row:
            row["init_cost_usd"] = restore_cost_usd(meta["memory_mb"])
        # Import lines are printed during INIT, so they belong to this log
        # stream's cold start when both arrive in the same batch.
        if row["is_cold"] and imports:
            row["n_imports"] = len(imports)
            row["log_stream"] = payload["logStream"]
        invocations.append(row)

    profiles = [
        {"function_id": function_id, "log_stream": payload["logStream"], **imp}
        for imp in imports
        if imp["cumulative_us"] >= MIN_IMPORT_US
    ]
    return invocations, profiles


def _dynamo(value):
    if isinstance(value, float):
        return Decimal(str(value))
    return value


def write_invocation(row):
    # UpdateItem so the runner's fields (run_id, strategy, ...) on the same
    # key are merged, not overwritten.
    key = {"function_id": row["function_id"], "request_id": row["request_id"]}
    fields = {k: _dynamo(v) for k, v in row.items() if k not in key}
    names = {f"#{k}": k for k in fields}
    values = {f":{k}": v for k, v in fields.items()}
    _table("INVOCATIONS_TABLE").update_item(
        Key=key,
        UpdateExpression="SET " + ", ".join(f"#{k} = :{k}" for k in fields),
        ExpressionAttributeNames=names,
        ExpressionAttributeValues=values,
    )


def emit_metrics(row):
    """CloudWatch Embedded Metric Format: one JSON line on stdout."""
    metrics = [{"Name": "ColdStart", "Unit": "Count"}]
    doc = {"workload": row["workload"], "arch": row["arch"], "packaging": row["packaging"], "ColdStart": int(row["is_cold"])}
    if row["is_cold"]:
        metrics.append({"Name": "InitDurationMs", "Unit": "Milliseconds"})
        metrics.append({"Name": "ColdStartCostUSD", "Unit": "None"})
        doc["InitDurationMs"] = row.get("init_ms", row.get("restore_ms", 0.0))
        doc["ColdStartCostUSD"] = row.get("init_cost_usd", 0.0)
    doc["_aws"] = {
        "Timestamp": int(datetime.now(tz=timezone.utc).timestamp() * 1000),
        "CloudWatchMetrics": [
            {
                "Namespace": "ColdStartObservatory",
                "Dimensions": [["workload", "arch", "packaging"]],
                "Metrics": metrics,
            }
        ],
    }
    print(json.dumps(doc))


def handler(event, context):
    payload = decode(event)
    if payload.get("messageType") != "DATA_MESSAGE":
        return {"invocations": 0, "imports": 0}

    invocations, profiles = build_rows(payload)
    for row in invocations:
        write_invocation(row)
        emit_metrics(row)
    if profiles:
        with _table("IMPORTS_TABLE").batch_writer(overwrite_by_pkeys=["function_id", "module"]) as batch:
            for p in profiles:
                batch.put_item(Item={k: _dynamo(v) for k, v in p.items()})
    return {"invocations": len(invocations), "imports": len(profiles)}
