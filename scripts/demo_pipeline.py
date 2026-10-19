"""End-to-end pipeline demo: force fresh cold starts and show every step's output.

    python scripts/demo_pipeline.py                      # pilot functions, 1 sample each
    python scripts/demo_pipeline.py --functions tiny-512-x86-zip --samples 2

Steps shown: unit tests -> runner -> function logs -> collector -> DynamoDB
-> import profiles -> CloudWatch metrics -> cost projection.
"""
import argparse
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import boto3

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "runner"), str(ROOT / "ml")]

import force_cold  # noqa: E402
from cost import monthly_cost_usd  # noqa: E402

PILOT = ["tiny-512-x86-zip", "web-512-x86-zip", "data-512-x86-zip"]
logs = boto3.client("logs")
cw = boto3.client("cloudwatch")
ddb = boto3.resource("dynamodb")


def step(n, title):
    print(f"\n{'=' * 70}\nSTEP {n}: {title}\n{'=' * 70}", flush=True)


def log_events(group, start_ms, pattern):
    events = []
    for page in logs.get_paginator("filter_log_events").paginate(
        logGroupName=group, startTime=start_ms, filterPattern=pattern
    ):
        events += page["events"]
    return events


def n_imports(function_id, log_stream):
    item = ddb.Table("import_profiles").get_item(
        Key={"function_id": function_id, "module": "#stream " + log_stream}
    ).get("Item")
    return int(item["n_imports"]) if item else 0


def wait_for_rows(run_id, expected, timeout_s=180):
    table = ddb.Table("invocations")
    deadline = time.time() + timeout_s
    while True:
        items = []
        kwargs = {}
        while True:
            page = table.scan(**kwargs)
            items += page["Items"]
            if "LastEvaluatedKey" not in page:
                break
            kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]
        rows = [r for r in items if r.get("run_id") == run_id and "init_ms" in r]
        if len(rows) >= expected or time.time() > deadline:
            return rows
        time.sleep(5)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--functions", nargs="+", default=PILOT)
    ap.add_argument("--samples", type=int, default=1)
    args = ap.parse_args()
    run_id = f"demo-{datetime.now(tz=timezone.utc):%Y%m%d-%H%M%S}"
    started = int(time.time() * 1000)

    step(1, "Unit tests (collector parser, no AWS)")
    subprocess.run([sys.executable, str(ROOT / "tests" / "test_collector.py")], check=True)
    sys.stdout.flush()

    step(2, f"Runner: force cold starts (run_id={run_id})")
    request_ids = {}
    for name in args.functions:
        for i in range(args.samples):
            t0 = time.time()
            rid = force_cold.force_cold_once(name, run_id, "on_demand")
            request_ids.setdefault(name, []).append(rid)
            print(f"  {name:<20} sample {i + 1}: request {rid}  ({time.time() - t0:.1f}s incl. config update)")

    step(3, "Function logs in CloudWatch (what the subscription filter forwards)")
    name = args.functions[-1]
    rid = request_ids[name][-1]
    time.sleep(10)
    events = log_events(f"/aws/lambda/{name}", started, '?REPORT ?"import time"')
    imports = [e["message"].rstrip() for e in events if "import time:" in e["message"]]
    report = next((e["message"].strip() for e in events if rid in e["message"] and "REPORT" in e["message"]), None)
    print(f"  {name}: {len(imports)} import-time lines, e.g.")
    for line in imports[-3:]:
        print(f"    {line}")
    print(f"  REPORT line:\n    {report}")

    step(4, "Collector Lambda: processed batches (its own log)")
    col = log_events("/aws/lambda/observatory-collector", started, "REPORT")
    print(f"  Collector ran {len(col)} times since the demo started. Last run:")
    if col:
        print(f"    {col[-1]['message'].strip()}")

    step(5, "DynamoDB `invocations`: runner + collector fields merged per request")
    expected = len(args.functions) * args.samples
    rows = wait_for_rows(run_id, expected)
    print(f"  {len(rows)}/{expected} rows complete\n")
    print(f"  {'function_id':<20} {'cold':<5} {'init_ms':>9} {'billed_ms':>9} {'max_mem':>7} {'pkg_MB':>7} {'imports':>7} {'init_cost_usd':>14}")
    for r in sorted(rows, key=lambda r: float(r["init_ms"])):
        print(
            f"  {r['function_id']:<20} {str(r['is_cold']):<5} {float(r['init_ms']):>9.1f} {float(r['billed_ms']):>9.0f}"
            f" {float(r['max_mem_mb']):>7.0f} {float(r.get('package_mb', 0)):>7.1f}"
            f" {n_imports(r['function_id'], r.get('log_stream', '')):>7}"
            f" {float(r['init_cost_usd']):>14.10f}"
        )

    step(6, "DynamoDB `import_profiles`: slowest imports (cumulative)")
    table = ddb.Table("import_profiles")
    for name in args.functions:
        items = table.query(KeyConditionExpression=boto3.dynamodb.conditions.Key("function_id").eq(name))["Items"]
        modules = [x for x in items if not x["module"].startswith("#stream ")]
        top = sorted(modules, key=lambda x: -int(x["cumulative_us"]))[:3]
        shown = ", ".join(f"{t['module']} {int(t['cumulative_us']) / 1000:.0f} ms" for t in top) or "(no module over 1 ms)"
        print(f"  {name:<20} {shown}")

    step(7, "CloudWatch metrics (namespace ColdStartObservatory, last hour)")
    now = datetime.now(tz=timezone.utc)
    for workload in sorted({n.split("-")[0] for n in args.functions}):
        dims = [{"Name": "workload", "Value": workload}, {"Name": "arch", "Value": "x86"}, {"Name": "packaging", "Value": "zip"}]
        stats = cw.get_metric_statistics(
            Namespace="ColdStartObservatory", MetricName="InitDurationMs", Dimensions=dims,
            StartTime=now - timedelta(hours=1), EndTime=now, Period=3600, Statistics=["Average", "SampleCount"],
        )["Datapoints"]
        if stats:
            d = stats[0]
            print(f"  InitDurationMs {workload:<5} avg {d['Average']:>7.1f} ms over {d['SampleCount']:.0f} cold starts")
        else:
            print(f"  InitDurationMs {workload:<5} (not aggregated yet; metrics can lag 1-2 min)")

    step(8, "Cost meter: monthly projection at 100k requests/month, 5% cold")
    print(f"  {'function_id':<20} {'on_demand':>10} {'snapstart':>10} {'provisioned':>12}")
    for r in sorted(rows, key=lambda r: r["function_id"]):
        args_ = dict(memory_mb=int(r["memory_mb"]), arch=r["arch"], requests_per_month=100_000,
                     avg_duration_ms=float(r["duration_ms"]), cold_rate=0.05, init_ms=float(r["init_ms"]), pc_concurrency=1)
        costs = [monthly_cost_usd(s, **args_)["total"] for s in ("on_demand", "snapstart", "provisioned")]
        print(f"  {r['function_id']:<20} {costs[0]:>9.2f}$ {costs[1]:>9.2f}$ {costs[2]:>11.2f}$")

    print(f"\nDone. Demo run_id: {run_id}")


if __name__ == "__main__":
    main()
