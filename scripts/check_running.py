"""List everything in the project that is billed while idle. Read-only.

    python scripts/check_running.py

Checks: provisioned concurrency, SnapStart-enabled functions, published
versions (SnapStart snapshots are billed per version), idle-gap schedules,
and ECR image storage. Idle ZIP functions, tables and log groups cost ~$0
and are not listed.
"""
import boto3

lam = boto3.client("lambda")


def main():
    billed = []
    functions = []
    for page in lam.get_paginator("list_functions").paginate():
        functions += [f["FunctionName"] for f in page["Functions"]]

    for name in sorted(functions):
        for page in lam.get_paginator("list_provisioned_concurrency_configs").paginate(FunctionName=name):
            for c in page["ProvisionedConcurrencyConfigs"]:
                billed.append(f"PROVISIONED CONCURRENCY  {name}:{c['FunctionArn'].rsplit(':', 1)[-1]} x{c['RequestedProvisionedConcurrentExecutions']}")
        cfg = lam.get_function_configuration(FunctionName=name)
        if cfg.get("SnapStart", {}).get("ApplyOn", "None") != "None":
            billed.append(f"SNAPSTART ON             {name}")
        versions = [v["Version"] for v in lam.list_versions_by_function(FunctionName=name)["Versions"] if v["Version"] != "$LATEST"]
        if versions:
            billed.append(f"PUBLISHED VERSIONS       {name}: {', '.join(versions)}")

    try:
        sched = boto3.client("scheduler")
        pending = [s["Name"] for p in sched.get_paginator("list_schedules").paginate(GroupName="observatory-idle-gap") for s in p["Schedules"]]
        if pending:
            billed.append(f"IDLE-GAP CHAINS RUNNING  {len(pending)} schedules (stop: python runner/idle_gap.py stop)")
    except sched.exceptions.ResourceNotFoundException:
        pass

    ecr = boto3.client("ecr")
    total = 0
    for page in ecr.get_paginator("describe_repositories").paginate():
        for repo in page["repositories"]:
            for ip in ecr.get_paginator("describe_images").paginate(repositoryName=repo["repositoryName"]):
                total += sum(i.get("imageSizeInBytes", 0) for i in ip["imageDetails"])
    if total:
        gb = total / 1024**3
        billed.append(f"ECR IMAGE STORAGE        {gb:.2f} GB (~${gb * 0.10:.2f}/month)")

    print(f"{len(functions)} Lambda functions in the account")
    if billed:
        print("Still billed while idle:")
        for line in billed:
            print(f"  {line}")
    else:
        print("Nothing billed while idle.")


if __name__ == "__main__":
    main()
