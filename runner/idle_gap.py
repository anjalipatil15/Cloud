"""Idle-gap experiment: how long can a function sit idle before Lambda recycles it?

Each target function runs its own chain: invoke it, record how long it sat
idle, then book the next invoke after a random gap (1-40 min) as a one-time
EventBridge Scheduler schedule that calls this Lambda again. The collector
fills in is_cold/init_ms for the same request id, so the dataset ends up with
(idle_gap_min -> is_cold) pairs for the cold-start probability model.

Start / stop chains from your laptop:
    python runner/idle_gap.py start --functions web-512-x86-zip data-512-x86-zip
    python runner/idle_gap.py status
    python runner/idle_gap.py stop

Don't run force_cold.py on the same functions while chains are running:
forcing a cold start resets the idle clock.
"""
import argparse
import json
import os
import random
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import boto3

GAPS_MIN = [1, 5, 10, 20, 40]
GROUP = os.environ.get("SCHEDULE_GROUP", "observatory-idle-gap")
STACK = "observatory"

lam = boto3.client("lambda")
scheduler = boto3.client("scheduler")


def _now():
    return datetime.now(tz=timezone.utc)


def schedule_next(function_id, run_id, last_invoke, target_arn, role_arn):
    gap = random.choice(GAPS_MIN)
    at = (_now() + timedelta(minutes=gap)).replace(microsecond=0)
    scheduler.create_schedule(
        Name=f"{function_id}-{uuid.uuid4().hex[:8]}",
        GroupName=GROUP,
        ScheduleExpression=f"at({at:%Y-%m-%dT%H:%M:%S})",
        ScheduleExpressionTimezone="UTC",
        FlexibleTimeWindow={"Mode": "OFF"},
        ActionAfterCompletion="DELETE",
        Target={
            "Arn": target_arn,
            "RoleArn": role_arn,
            "Input": json.dumps({"function_id": function_id, "run_id": run_id, "last_invoke": last_invoke}),
        },
    )
    return gap


def handler(event, context):
    """One link of a chain (runs in Lambda)."""
    function_id, run_id = event["function_id"], event["run_id"]
    now = _now()
    resp = lam.invoke(FunctionName=function_id, Payload=b"{}")
    request_id = resp["ResponseMetadata"]["RequestId"]

    fields = {":r": run_id, ":s": "on_demand", ":f": False}
    expr = "SET run_id = :r, strategy = :s, forced = :f"
    if event.get("last_invoke"):
        idle = (now - datetime.fromisoformat(event["last_invoke"])).total_seconds() / 60
        fields[":g"] = Decimal(str(round(idle, 2)))
        expr += ", idle_gap_min = :g"
    boto3.resource("dynamodb").Table(os.environ["INVOCATIONS_TABLE"]).update_item(
        Key={"function_id": function_id, "request_id": request_id},
        UpdateExpression=expr,
        ExpressionAttributeValues=fields,
    )

    gap = schedule_next(function_id, run_id, now.isoformat(), context.invoked_function_arn, os.environ["SCHEDULER_ROLE_ARN"])
    print(json.dumps({"function_id": function_id, "request_id": request_id, "next_gap_min": gap}))
    return {"request_id": request_id, "next_gap_min": gap}


# ---- CLI (runs on your laptop) -------------------------------------------

def _stack_outputs():
    stack = boto3.client("cloudformation").describe_stacks(StackName=STACK)["Stacks"][0]
    return {o["OutputKey"]: o["OutputValue"] for o in stack.get("Outputs", [])}


def _schedules():
    names = []
    for page in scheduler.get_paginator("list_schedules").paginate(GroupName=GROUP):
        names += [s["Name"] for s in page["Schedules"]]
    return names


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    start = sub.add_parser("start")
    start.add_argument("--functions", nargs="+", required=True)
    start.add_argument("--run-id", default=f"idle-gap-{_now():%Y%m%d}")
    sub.add_parser("status")
    sub.add_parser("stop")
    args = ap.parse_args()

    if args.cmd == "start":
        out = _stack_outputs()
        for f in args.functions:
            # First link: invoke via the idle-gap Lambda so all records come from one place.
            lam.invoke(
                FunctionName=out["IdleGapFunctionArn"],
                InvocationType="Event",
                Payload=json.dumps({"function_id": f, "run_id": args.run_id}).encode(),
            )
            print(f"started chain for {f} (run_id={args.run_id})")
    elif args.cmd == "status":
        names = _schedules()
        print(f"{len(names)} pending schedules in {GROUP}")
        for n in sorted(names):
            print(f"  {n}")
    elif args.cmd == "stop":
        names = _schedules()
        for n in names:
            scheduler.delete_schedule(Name=n, GroupName=GROUP)
        print(f"deleted {len(names)} schedules; chains stopped")


if __name__ == "__main__":
    main()
