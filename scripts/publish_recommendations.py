"""Publish the `recommendations` table to Grafana.

Grafana's CloudWatch data source can't read DynamoDB, so this copies every
recommendation row into the log group /observatory/recommendations as one JSON
line each. The dashboard's "Strategy recommendations" panel reads the latest
line per function with Logs Insights.

Run after ml/recommender.py has written the table:
    python scripts/publish_recommendations.py
"""
import json
import time
from decimal import Decimal

import boto3

LOG_GROUP = "/observatory/recommendations"

logs = boto3.client("logs")


def _plain(value):
    return float(value) if isinstance(value, Decimal) else value


def main():
    rows = []
    table = boto3.resource("dynamodb").Table("recommendations")
    kwargs = {}
    while True:
        page = table.scan(**kwargs)
        rows += page["Items"]
        if "LastEvaluatedKey" not in page:
            break
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]
    if not rows:
        print("recommendations table is empty; nothing to publish")
        return

    try:
        logs.create_log_group(logGroupName=LOG_GROUP)
        logs.put_retention_policy(logGroupName=LOG_GROUP, retentionInDays=30)
    except logs.exceptions.ResourceAlreadyExistsException:
        pass
    stream = f"publish-{time.strftime('%Y%m%d-%H%M%S')}"
    logs.create_log_stream(logGroupName=LOG_GROUP, logStreamName=stream)

    # Logs Insights skips events stamped before the log group was created. A laptop
    # clock slightly behind AWS's did exactly that on the first publish (events 0.6 s
    # "older" than the group), so never stamp earlier than the group's creation time.
    created = logs.describe_log_groups(logGroupNamePrefix=LOG_GROUP)["logGroups"][0]["creationTime"]
    now = max(int(time.time() * 1000), created + 1000)
    events = [
        {"timestamp": now, "message": json.dumps({"type": "recommendation", **{k: _plain(v) for k, v in r.items()}})}
        for r in sorted(rows, key=lambda r: r["function_id"])
    ]
    logs.put_log_events(logGroupName=LOG_GROUP, logStreamName=stream, logEvents=events)
    print(f"published {len(events)} recommendations to {LOG_GROUP}")


if __name__ == "__main__":
    main()
