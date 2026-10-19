"""Generate infra/template.yaml (SAM) for a function matrix.

One stack ("observatory") holds the tables, the collector and every test
function; redeploying with a bigger matrix adds functions.

    python infra/generate_template.py --matrix pilot   # or core, full (core + packaging)
    sam build --use-container && sam deploy

Each test function gets: the function, its log group (7-day retention) and a
subscription filter to the collector. All test functions share one role.
"""
import argparse
import itertools
import shutil
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
GEN = ROOT / "infra" / ".gen"

MATRICES = {
    # Stage 1: one function per workload, smallest set that proves the pipeline.
    "pilot": [("tiny", 512, "x86", "zip"), ("web", 512, "x86", "zip"), ("data", 512, "x86", "zip")],
    # Stage 2 core sweep: workloads x memory x arch, ZIP only. `data` and `ml`
    # skip 128 MB: importing pandas alone peaks at ~130 MB, so INIT would run out of memory.
    "core": [
        combo
        for combo in itertools.product(["tiny", "web", "data", "ml"], [128, 256, 512, 1024, 2048], ["x86", "arm64"], ["zip"])
        if not (combo[0] in ("data", "ml") and combo[1] == 128)
    ],
    # Stage 2 packaging experiment: `data` as layer and container image (ZIP is in core).
    "packaging": list(itertools.product(["data"], [512, 1024], ["x86", "arm64"], ["layer", "image"])),
}
# One stack holds everything, so each deploy must list every matrix still wanted.
MATRICES["full"] = MATRICES["core"] + MATRICES["packaging"]
ARCH = {"x86": "x86_64", "arm64": "arm64"}


def function_id(workload, memory, arch, packaging):
    return f"{workload}-{memory}-{arch}-{packaging}"


def logical(fid):
    # CloudFormation logical IDs are alphanumeric only.
    return "".join(part.capitalize() for part in fid.split("-"))


def base_resources():
    table = lambda name, keys: {  # noqa: E731
        "Type": "AWS::DynamoDB::Table",
        "Properties": {
            "TableName": name,
            "BillingMode": "PAY_PER_REQUEST",
            "AttributeDefinitions": [{"AttributeName": k, "AttributeType": "S"} for k, _ in keys],
            "KeySchema": [{"AttributeName": k, "KeyType": t} for k, t in keys],
        },
    }
    return {
        "InvocationsTable": table("invocations", [("function_id", "HASH"), ("request_id", "RANGE")]),
        "ImportProfilesTable": table("import_profiles", [("function_id", "HASH"), ("module", "RANGE")]),
        "RecommendationsTable": table("recommendations", [("function_id", "HASH")]),
        "TestFunctionRole": {
            "Type": "AWS::IAM::Role",
            "Properties": {
                "AssumeRolePolicyDocument": {
                    "Version": "2012-10-17",
                    "Statement": [{"Effect": "Allow", "Principal": {"Service": "lambda.amazonaws.com"}, "Action": "sts:AssumeRole"}],
                },
                "ManagedPolicyArns": ["arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"],
            },
        },
        "CollectorFunction": {
            "Type": "AWS::Serverless::Function",
            "Properties": {
                "FunctionName": "observatory-collector",
                "CodeUri": "../collector/",
                "Handler": "app.handler",
                "Runtime": "python3.12",
                "Architectures": ["arm64"],
                "MemorySize": 256,
                "Timeout": 60,
                "Environment": {
                    "Variables": {
                        "INVOCATIONS_TABLE": {"Ref": "InvocationsTable"},
                        "IMPORTS_TABLE": {"Ref": "ImportProfilesTable"},
                    }
                },
                "Policies": [
                    {"DynamoDBCrudPolicy": {"TableName": {"Ref": "InvocationsTable"}}},
                    {"DynamoDBCrudPolicy": {"TableName": {"Ref": "ImportProfilesTable"}}},
                ],
            },
        },
        "CollectorLogGroup": {
            "Type": "AWS::Logs::LogGroup",
            "Properties": {"LogGroupName": "/aws/lambda/observatory-collector", "RetentionInDays": 7},
        },
        # Idle-gap experiment (runner/idle_gap.py): a Lambda that invokes a test
        # function and books its own next run via one-time Scheduler schedules.
        "IdleGapScheduleGroup": {
            "Type": "AWS::Scheduler::ScheduleGroup",
            "Properties": {"Name": "observatory-idle-gap"},
        },
        "IdleGapSchedulerRole": {
            "Type": "AWS::IAM::Role",
            "Properties": {
                "AssumeRolePolicyDocument": {
                    "Version": "2012-10-17",
                    "Statement": [{"Effect": "Allow", "Principal": {"Service": "scheduler.amazonaws.com"}, "Action": "sts:AssumeRole"}],
                },
                "Policies": [
                    {
                        "PolicyName": "invoke-idle-gap",
                        "PolicyDocument": {
                            "Version": "2012-10-17",
                            "Statement": [{"Effect": "Allow", "Action": "lambda:InvokeFunction", "Resource": {"Fn::Sub": "arn:aws:lambda:${AWS::Region}:${AWS::AccountId}:function:observatory-idle-gap"}}],
                        },
                    }
                ],
            },
        },
        "IdleGapFunction": {
            "Type": "AWS::Serverless::Function",
            "Properties": {
                "FunctionName": "observatory-idle-gap",
                "CodeUri": "../runner/",
                "Handler": "idle_gap.handler",
                "Runtime": "python3.12",
                "Architectures": ["arm64"],
                "MemorySize": 128,
                "Timeout": 60,
                "Environment": {
                    "Variables": {
                        "INVOCATIONS_TABLE": {"Ref": "InvocationsTable"},
                        "SCHEDULER_ROLE_ARN": {"Fn::GetAtt": ["IdleGapSchedulerRole", "Arn"]},
                        "SCHEDULE_GROUP": {"Ref": "IdleGapScheduleGroup"},
                    }
                },
                "Policies": [
                    {"DynamoDBCrudPolicy": {"TableName": {"Ref": "InvocationsTable"}}},
                    {
                        "Statement": [
                            {"Effect": "Allow", "Action": "lambda:InvokeFunction", "Resource": {"Fn::Sub": "arn:aws:lambda:${AWS::Region}:${AWS::AccountId}:function:*"}},
                            {"Effect": "Allow", "Action": "scheduler:CreateSchedule", "Resource": {"Fn::Sub": "arn:aws:scheduler:${AWS::Region}:${AWS::AccountId}:schedule/observatory-idle-gap/*"}},
                            {"Effect": "Allow", "Action": "iam:PassRole", "Resource": {"Fn::GetAtt": ["IdleGapSchedulerRole", "Arn"]}},
                        ]
                    },
                ],
            },
        },
        "IdleGapLogGroup": {
            "Type": "AWS::Logs::LogGroup",
            "Properties": {"LogGroupName": "/aws/lambda/observatory-idle-gap", "RetentionInDays": 7},
        },
        "CollectorInvokePermission": {
            "Type": "AWS::Lambda::Permission",
            "Properties": {
                "FunctionName": {"Ref": "CollectorFunction"},
                "Action": "lambda:InvokeFunction",
                "Principal": "logs.amazonaws.com",
                "SourceArn": {"Fn::Sub": "arn:aws:logs:${AWS::Region}:${AWS::AccountId}:log-group:/aws/lambda/*"},
            },
        },
    }


def prepare_packaging_dirs(combos):
    """Write build inputs for layer/image variants into infra/.gen/ (gitignored).

    layer: handler-only code dir + a layer dir holding requirements.txt
    image: Dockerfile context with the handler and requirements
    """
    for workload in sorted({c[0] for c in combos if c[3] in ("layer", "image")}):
        src = ROOT / "workloads" / workload
        code = GEN / f"{workload}-code"
        layer = GEN / f"{workload}-layer"
        image = GEN / f"{workload}-image"
        for d in (code, layer, image):
            d.mkdir(parents=True, exist_ok=True)
        shutil.copy(src / "app.py", code / "app.py")
        shutil.copy(src / "requirements.txt", layer / "requirements.txt")
        shutil.copy(src / "app.py", image / "app.py")
        shutil.copy(src / "requirements.txt", image / "requirements.txt")
        (image / "Dockerfile").write_text(
            "FROM public.ecr.aws/lambda/python:3.12\n"
            "COPY requirements.txt .\n"
            "RUN pip install --no-cache-dir -r requirements.txt -t ${LAMBDA_TASK_ROOT}\n"
            "COPY app.py ${LAMBDA_TASK_ROOT}\n"
            'CMD ["app.handler"]\n'
        )


def layer_resources(combos):
    """One layer per (workload, arch) used by a layer variant."""
    out = {}
    for workload, arch in sorted({(c[0], c[2]) for c in combos if c[3] == "layer"}):
        out[logical(f"{workload}-{arch}-deps-layer")] = {
            "Type": "AWS::Serverless::LayerVersion",
            "Properties": {
                "LayerName": f"observatory-{workload}-{arch}-deps",
                "ContentUri": f".gen/{workload}-layer/",
                "CompatibleRuntimes": ["python3.12"],
                "CompatibleArchitectures": [ARCH[arch]],
            },
            "Metadata": {"BuildMethod": "python3.12", "BuildArchitecture": ARCH[arch]},
        }
    return out


def test_function_resources(workload, memory, arch, packaging):
    fid = function_id(workload, memory, arch, packaging)
    name = logical(fid)
    props = {
        "FunctionName": fid,
        "Architectures": [ARCH[arch]],
        "MemorySize": memory,
        "Role": {"Fn::GetAtt": ["TestFunctionRole", "Arn"]},
        "Environment": {"Variables": {"PYTHONPROFILEIMPORTTIME": "1", "BUST": "deploy"}},
        "Tags": {"workload": workload, "arch": arch, "packaging": packaging},
    }
    function = {"Type": "AWS::Serverless::Function", "Properties": props}
    if packaging == "zip":
        props.update({"CodeUri": f"../workloads/{workload}/", "Handler": "app.handler", "Runtime": "python3.12"})
    elif packaging == "layer":
        props.update({
            "CodeUri": f".gen/{workload}-code/",
            "Handler": "app.handler",
            "Runtime": "python3.12",
            "Layers": [{"Ref": logical(f"{workload}-{arch}-deps-layer")}],
        })
    elif packaging == "image":
        props["PackageType"] = "Image"
        function["Metadata"] = {"Dockerfile": "Dockerfile", "DockerContext": f".gen/{workload}-image", "DockerTag": f"{arch}"}
    return {
        f"{name}Function": function,
        f"{name}LogGroup": {
            "Type": "AWS::Logs::LogGroup",
            "Properties": {"LogGroupName": f"/aws/lambda/{fid}", "RetentionInDays": 7},
        },
        f"{name}Subscription": {
            "Type": "AWS::Logs::SubscriptionFilter",
            "DependsOn": ["CollectorInvokePermission"],
            "Properties": {
                "LogGroupName": {"Ref": f"{name}LogGroup"},
                "FilterPattern": '?REPORT ?"import time"',
                "DestinationArn": {"Fn::GetAtt": ["CollectorFunction", "Arn"]},
            },
        },
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--matrix", choices=sorted(MATRICES), default="pilot")
    args = ap.parse_args()

    # The collector ships the cost model; ml/ stays the single source of prices.
    for name in ("cost.py", "pricing.json"):
        shutil.copy(ROOT / "ml" / name, ROOT / "collector" / name)

    combos = MATRICES[args.matrix]
    prepare_packaging_dirs(combos)
    resources = base_resources()
    resources.update(layer_resources(combos))
    for combo in combos:
        resources.update(test_function_resources(*combo))

    template = {
        "AWSTemplateFormatVersion": "2010-09-09",
        "Transform": "AWS::Serverless-2016-10-31",
        "Description": f"Serverless Cold-Start Observatory ({args.matrix} matrix)",
        "Globals": {"Function": {"Timeout": 30}},
        "Resources": resources,
        "Outputs": {
            "FunctionIds": {"Value": ",".join(function_id(*c) for c in combos)},
            "IdleGapFunctionArn": {"Value": {"Fn::GetAtt": ["IdleGapFunction", "Arn"]}},
        },
    }
    out = ROOT / "infra" / "template.yaml"
    out.write_text("# GENERATED by infra/generate_template.py - do not edit by hand\n" + yaml.safe_dump(template, sort_keys=False))
    print(f"Wrote {out} with {len(combos)} test functions ({len(resources)} resources)")


if __name__ == "__main__":
    main()
