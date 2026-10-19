"""Strategy experiment: the same function under SnapStart or provisioned concurrency.

On-demand samples come from force_cold.py; this script adds the other two.
Both cost money while enabled, so every run cleans up after itself
(and `cleanup` removes anything left behind by a crashed run).

    python runner/strategy.py snapstart   --functions web-512-x86-zip data-512-x86-zip --samples 30
    python runner/strategy.py provisioned --functions web-512-x86-zip --samples 30
    python runner/strategy.py cleanup     --functions web-512-x86-zip data-512-x86-zip

SnapStart: each published version gets a new snapshot. A burst of parallel
invokes on a fresh version lands on new environments, each restored from the
snapshot (REPORT shows `Restore Duration`). Repeat with new versions until
enough samples.

Provisioned concurrency: environments are initialised ahead of time, so
requests never see INIT. Needs unreserved concurrency >= 10 + the amount
provisioned, i.e. an account limit above 10.
"""
import argparse
import json
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from decimal import Decimal

import boto3
from botocore.config import Config

lam = boto3.client("lambda", config=Config(retries={"max_attempts": 10, "mode": "adaptive"}, max_pool_connections=32))
invocations = boto3.resource("dynamodb").Table("invocations")


def tag(function_id, request_id, run_id, strategy, client_ms, version):
    invocations.update_item(
        Key={"function_id": function_id, "request_id": request_id},
        UpdateExpression="SET run_id = :r, strategy = :s, client_ms = :c, version = :v, forced = :f",
        ExpressionAttributeValues={
            ":r": run_id, ":s": strategy, ":c": Decimal(str(round(client_ms, 2))), ":v": version, ":f": True,
        },
    )


def invoke(function_id, qualifier):
    t0 = time.perf_counter()
    resp = lam.invoke(FunctionName=function_id, Qualifier=qualifier, Payload=b"{}")
    client_ms = (time.perf_counter() - t0) * 1000
    if resp.get("FunctionError"):
        raise RuntimeError(f"{function_id}:{qualifier}: {resp['Payload'].read().decode()}")
    return resp["ResponseMetadata"]["RequestId"], client_ms


def publish_new_version(function_id):
    """Change BUST so the code/config hash differs, then publish a version."""
    cfg = lam.get_function_configuration(FunctionName=function_id)
    env = cfg.get("Environment", {}).get("Variables", {})
    env["BUST"] = uuid.uuid4().hex
    lam.update_function_configuration(FunctionName=function_id, Environment={"Variables": env})
    lam.get_waiter("function_updated_v2").wait(FunctionName=function_id)
    version = lam.publish_version(FunctionName=function_id)["Version"]
    # SnapStart: the version stays Pending until its snapshot is ready (can take minutes).
    lam.get_waiter("published_version_active").wait(FunctionName=function_id, Qualifier=version)
    return version


def delete_versions(function_id):
    versions = []
    for page in lam.get_paginator("list_versions_by_function").paginate(FunctionName=function_id):
        versions += [v["Version"] for v in page["Versions"] if v["Version"] != "$LATEST"]
    for v in versions:
        lam.delete_function(FunctionName=function_id, Qualifier=v)
    return versions


def run_snapstart(function_id, samples, burst, run_id):
    lam.update_function_configuration(FunctionName=function_id, SnapStart={"ApplyOn": "PublishedVersions"})
    lam.get_waiter("function_updated_v2").wait(FunctionName=function_id)
    done = 0
    try:
        while done < samples:
            n = min(burst, samples - done)
            version = publish_new_version(function_id)
            with ThreadPoolExecutor(max_workers=n) as pool:
                results = list(pool.map(lambda _: invoke(function_id, version), range(n)))
            for request_id, client_ms in results:
                tag(function_id, request_id, run_id, "snapstart", client_ms, version)
            done += n
            print(f"{function_id} v{version}: {n} invokes ({done}/{samples})", flush=True)
            # Old versions keep a billed snapshot cache; drop each once used.
            lam.delete_function(FunctionName=function_id, Qualifier=version)
    finally:
        cleanup(function_id)


def run_provisioned(function_id, samples, run_id, concurrency=1):
    limit = lam.get_account_settings()["AccountLimit"]["ConcurrentExecutions"]
    if limit < 10 + concurrency + 1:
        raise SystemExit(
            f"Account concurrency limit is {limit}; Lambda keeps 10 unreserved, so provisioned "
            "concurrency is not possible until the quota increase is approved."
        )
    version = publish_new_version(function_id)
    try:
        lam.put_provisioned_concurrency_config(
            FunctionName=function_id, Qualifier=version, ProvisionedConcurrentExecutions=concurrency
        )
        while lam.get_provisioned_concurrency_config(FunctionName=function_id, Qualifier=version)["Status"] != "READY":
            time.sleep(10)
        for i in range(samples):
            request_id, client_ms = invoke(function_id, version)
            tag(function_id, request_id, run_id, "provisioned", client_ms, version)
            print(f"{function_id} v{version} [{i + 1}/{samples}] {client_ms:.0f} ms", flush=True)
    finally:
        cleanup(function_id)


def cleanup(function_id):
    """Remove everything that is billed by the hour for this function."""
    for page in lam.get_paginator("list_provisioned_concurrency_configs").paginate(FunctionName=function_id):
        for c in page["ProvisionedConcurrencyConfigs"]:
            qualifier = c["FunctionArn"].rsplit(":", 1)[-1]
            lam.delete_provisioned_concurrency_config(FunctionName=function_id, Qualifier=qualifier)
    cfg = lam.get_function_configuration(FunctionName=function_id)
    if cfg.get("SnapStart", {}).get("ApplyOn") != "None":
        lam.update_function_configuration(FunctionName=function_id, SnapStart={"ApplyOn": "None"})
        lam.get_waiter("function_updated_v2").wait(FunctionName=function_id)
    deleted = delete_versions(function_id)
    print(json.dumps({"cleaned": function_id, "deleted_versions": deleted}))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("strategy", choices=["snapstart", "provisioned", "cleanup"])
    ap.add_argument("--functions", nargs="+", required=True)
    ap.add_argument("--samples", type=int, default=30)
    ap.add_argument("--burst", type=int, default=8, help="SnapStart: parallel invokes per fresh version")
    ap.add_argument("--run-id", default=f"strategy-{datetime.now(tz=timezone.utc):%Y%m%d-%H%M}")
    args = ap.parse_args()

    for f in args.functions:
        if args.strategy == "snapstart":
            run_snapstart(f, args.samples, args.burst, args.run_id)
        elif args.strategy == "provisioned":
            run_provisioned(f, args.samples, args.run_id)
        else:
            cleanup(f)


if __name__ == "__main__":
    main()
