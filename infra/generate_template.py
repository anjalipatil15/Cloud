"""Generate infra/template.yaml (SAM) for a function matrix.

One stack ("observatory") holds the tables, the collector and every test
function; redeploying with a bigger matrix adds functions.

    python infra/generate_template.py --matrix pilot
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

MATRICES = {
    # Stage 1: one function per workload, smallest set that proves the pipeline.
    "pilot": [("tiny", 512, "x86", "zip"), ("web", 512, "x86", "zip"), ("data", 512, "x86", "zip")],
    # Stage 2 core sweep: workloads x memory x arch, ZIP only.
    "core": list(itertools.product(["tiny", "web", "data", "ml"], [128, 256, 512, 1024, 2048], ["x86", "arm64"], ["zip"])),
}
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


def test_function_resources(workload, memory, arch, packaging):
    if packaging != "zip":
        raise NotImplementedError("layer/image packaging comes in Stage 2")
    fid = function_id(workload, memory, arch, packaging)
    name = logical(fid)
    return {
        f"{name}Function": {
            "Type": "AWS::Serverless::Function",
            "Properties": {
                "FunctionName": fid,
                "CodeUri": f"../workloads/{workload}/",
                "Handler": "app.handler",
                "Architectures": [ARCH[arch]],
                "MemorySize": memory,
                "Role": {"Fn::GetAtt": ["TestFunctionRole", "Arn"]},
                "Environment": {"Variables": {"PYTHONPROFILEIMPORTTIME": "1", "BUST": "deploy"}},
                "Tags": {"workload": workload, "arch": arch, "packaging": packaging},
            },
        },
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

    resources = base_resources()
    for combo in MATRICES[args.matrix]:
        resources.update(test_function_resources(*combo))

    template = {
        "AWSTemplateFormatVersion": "2010-09-09",
        "Transform": "AWS::Serverless-2016-10-31",
        "Description": f"Serverless Cold-Start Observatory ({args.matrix} matrix)",
        "Globals": {"Function": {"Runtime": "python3.12", "Timeout": 30}},
        "Resources": resources,
        "Outputs": {
            "FunctionIds": {"Value": ",".join(function_id(*c) for c in MATRICES[args.matrix])},
        },
    }
    out = ROOT / "infra" / "template.yaml"
    out.write_text("# GENERATED by infra/generate_template.py - do not edit by hand\n" + yaml.safe_dump(template, sort_keys=False))
    print(f"Wrote {out} with {len(MATRICES[args.matrix])} test functions ({len(resources)} resources)")


if __name__ == "__main__":
    main()
