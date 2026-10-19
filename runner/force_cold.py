"""Force cold starts and tag each invocation with experiment metadata.

For each sample: change the dummy env var BUST (Lambda then discards warm
environments), wait for the update, invoke once. The REPORT line of that
invocation has `Init Duration`; the collector writes it to `invocations`, and
this script merges run_id/strategy/package_mb into the same row.

    python runner/force_cold.py --functions tiny-512-x86-zip data-512-x86-zip --samples 3
    python runner/force_cold.py --all --samples 30 --threads 8
"""
import argparse
import json
import re
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from decimal import Decimal

import boto3
from botocore.config import Config

FUNCTION_ID = re.compile(r"^(tiny|web|data|ml)-(\d+)-(x86|arm64)-(zip|layer|image)$")

lam = boto3.client("lambda", config=Config(retries={"max_attempts": 10, "mode": "adaptive"}))
invocations = boto3.resource("dynamodb").Table("invocations")


def list_test_functions():
    names = []
    for page in lam.get_paginator("list_functions").paginate():
        names += [f["FunctionName"] for f in page["Functions"] if FUNCTION_ID.match(f["FunctionName"])]
    return sorted(names)


def force_cold_once(name, run_id, strategy):
    cfg = lam.get_function_configuration(FunctionName=name)
    env = cfg.get("Environment", {}).get("Variables", {})
    env["BUST"] = uuid.uuid4().hex  # update replaces the whole map, so keep the rest
    lam.update_function_configuration(FunctionName=name, Environment={"Variables": env})
    lam.get_waiter("function_updated_v2").wait(FunctionName=name)

    resp = lam.invoke(FunctionName=name, Payload=json.dumps({"run_id": run_id}).encode())
    if resp.get("FunctionError"):
        raise RuntimeError(f"{name}: {resp['Payload'].read().decode()}")
    request_id = resp["ResponseMetadata"]["RequestId"]  # same id as in the REPORT line

    invocations.update_item(
        Key={"function_id": name, "request_id": request_id},
        UpdateExpression="SET run_id = :r, strategy = :s, package_mb = :p, forced = :f",
        ExpressionAttributeValues={
            ":r": run_id,
            ":s": strategy,
            ":p": Decimal(str(round(cfg["CodeSize"] / 1024 / 1024, 3))),
            ":f": True,
        },
    )
    return request_id


def run_function(name, samples, run_id, strategy):
    # Samples for one function are sequential (each needs its own config update).
    for i in range(samples):
        rid = force_cold_once(name, run_id, strategy)
        print(f"{name} [{i + 1}/{samples}] {rid}", flush=True)


def main():
    ap = argparse.ArgumentParser()
    sel = ap.add_mutually_exclusive_group(required=True)
    sel.add_argument("--functions", nargs="+")
    sel.add_argument("--all", action="store_true", help="every deployed test function")
    ap.add_argument("--samples", type=int, default=3)
    ap.add_argument("--threads", type=int, default=1, help="functions run in parallel")
    ap.add_argument("--strategy", default="on_demand", choices=["on_demand", "snapstart", "provisioned"])
    ap.add_argument("--run-id", default=f"run-{datetime.now(tz=timezone.utc):%Y%m%d-%H%M}")
    args = ap.parse_args()

    names = list_test_functions() if args.all else args.functions
    print(f"run_id={args.run_id}: {len(names)} functions x {args.samples} samples")
    with ThreadPoolExecutor(max_workers=args.threads) as pool:
        for f in [pool.submit(run_function, n, args.samples, args.run_id, args.strategy) for n in names]:
            f.result()


if __name__ == "__main__":
    main()
