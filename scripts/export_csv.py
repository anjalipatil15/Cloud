"""Export the DynamoDB tables to CSV for analysis (run after every experiment).

    python scripts/export_csv.py              # writes data/*.csv
    python scripts/export_csv.py --out exports

Files:
  invocations.csv      one row per invocation; n_imports joined in from the
                       per-stream counters in import_profiles
  import_profiles.csv  module timings (counter rows left out)
"""
import argparse
import csv
from pathlib import Path

import boto3

ROOT = Path(__file__).resolve().parents[1]
STREAM_PREFIX = "#stream "

INVOCATION_COLUMNS = [
    "function_id", "request_id", "ts", "run_id", "strategy", "forced",
    "workload", "memory_mb", "arch", "packaging", "package_mb", "n_imports",
    "idle_gap_min", "is_cold", "init_ms", "restore_ms", "billed_restore_ms",
    "duration_ms", "billed_ms", "max_mem_mb", "init_cost_usd", "client_ms", "version", "log_stream",
]
PROFILE_COLUMNS = ["function_id", "module", "self_us", "cumulative_us", "log_stream", "ts"]


def scan(table_name):
    table = boto3.resource("dynamodb").Table(table_name)
    items, kwargs = [], {}
    while True:
        page = table.scan(**kwargs)
        items += page["Items"]
        if "LastEvaluatedKey" not in page:
            return items
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def write(path, columns, rows):
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    print(f"{path}: {len(rows)} rows")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "data"))
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    profiles = scan("import_profiles")
    counts = {
        (p["function_id"], p["module"][len(STREAM_PREFIX):]): p["n_imports"]
        for p in profiles
        if p["module"].startswith(STREAM_PREFIX)
    }
    modules = [p for p in profiles if not p["module"].startswith(STREAM_PREFIX)]

    invocations = scan("invocations")
    for row in invocations:
        n = counts.get((row["function_id"], row.get("log_stream")))
        if n is not None:
            row["n_imports"] = n
    invocations.sort(key=lambda r: (r["function_id"], r.get("ts", "")))

    write(out / "invocations.csv", INVOCATION_COLUMNS, invocations)
    write(out / "import_profiles.csv", PROFILE_COLUMNS, sorted(modules, key=lambda p: (p["function_id"], -int(p["cumulative_us"]))))


if __name__ == "__main__":
    main()
