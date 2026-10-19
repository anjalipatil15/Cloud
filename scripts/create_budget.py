"""Create (or replace) the project's monthly cost budget with email alerts.

Alerts at 50% and 100% of actual spend, and at 100% of forecast. Usage:
    python scripts/create_budget.py you@example.com [limit_usd]
"""
import sys

import boto3

BUDGET_NAME = "observatory-monthly"


def main():
    email = sys.argv[1]
    limit = sys.argv[2] if len(sys.argv) > 2 else "5"
    account = boto3.client("sts").get_caller_identity()["Account"]
    budgets = boto3.client("budgets", region_name="us-east-1")

    try:
        budgets.delete_budget(AccountId=account, BudgetName=BUDGET_NAME)
        print(f"Replaced existing budget {BUDGET_NAME}")
    except budgets.exceptions.NotFoundException:
        pass

    subscriber = [{"SubscriptionType": "EMAIL", "Address": email}]
    alerts = [("ACTUAL", 50), ("ACTUAL", 100), ("FORECASTED", 100)]
    budgets.create_budget(
        AccountId=account,
        Budget={
            "BudgetName": BUDGET_NAME,
            "BudgetType": "COST",
            "TimeUnit": "MONTHLY",
            "BudgetLimit": {"Amount": limit, "Unit": "USD"},
        },
        NotificationsWithSubscribers=[
            {
                "Notification": {
                    "NotificationType": kind,
                    "ComparisonOperator": "GREATER_THAN",
                    "Threshold": pct,
                    "ThresholdType": "PERCENTAGE",
                },
                "Subscribers": subscriber,
            }
            for kind, pct in alerts
        ],
    )
    print(f"Budget {BUDGET_NAME}: ${limit}/month, alerts to {email}")


if __name__ == "__main__":
    main()
