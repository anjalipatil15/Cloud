"""Stage 0 read-only account check: identity, region, Lambda quotas, budget.

Makes no changes and costs nothing. Usage:
    python scripts/check_account.py            # uses AWS_REGION / default profile
    AWS_PROFILE=obs-infra python scripts/check_account.py
"""
import boto3
from botocore.exceptions import ClientError

# SnapStart for Python needs python3.12+ and is in all commercial regions
# except these (Lambda developer guide, 2026).
SNAPSTART_EXCLUDED = {"ap-southeast-6", "ap-east-2"}  # New Zealand, Taipei


def main():
    session = boto3.session.Session()
    region = session.region_name
    ident = session.client("sts").get_caller_identity()
    print(f"Account:  {ident['Account']}")
    print(f"Caller:   {ident['Arn']}")
    print(f"Region:   {region}")

    lam = session.client("lambda")
    settings = lam.get_account_settings()
    limit = settings["AccountLimit"]
    print(f"\nLambda concurrency limit:      {limit['ConcurrentExecutions']}")
    print(f"Unreserved concurrency:        {limit['UnreservedConcurrentExecutions']}")
    print(f"Functions deployed:            {settings['AccountUsage']['FunctionCount']}")
    if limit["ConcurrentExecutions"] < 100:
        print("  -> Low quota (new accounts often get 10). The 8-thread runner and burst")
        print("     trick need more: request 1000 in Service Quotas (L-B99A9384).")

    print(f"\nSnapStart (Python) available:  {'no' if region in SNAPSTART_EXCLUDED else 'yes'}")

    try:
        budgets = session.client("budgets", region_name="us-east-1").describe_budgets(
            AccountId=ident["Account"]
        ).get("Budgets", [])
        print(f"\nBudgets: {len(budgets)}")
        for b in budgets:
            print(f"  {b['BudgetName']}: {b['BudgetLimit']['Amount']} {b['BudgetLimit']['Unit']}")
        if not budgets:
            print("  -> No budget yet. Deploy infra/account-setup.yaml.")
    except ClientError as e:
        print(f"\nBudgets: could not read ({e.response['Error']['Code']})")


if __name__ == "__main__":
    main()
