# Getting access to the project's AWS account

All project resources live in one shared AWS account. You don't need your own
AWS account: Anjali has created a login for you inside the project account.
Anything you do there is billed to the project account, not to you.

## Your login

| Role | Your user name | You own |
|---|---|---|
| A: Infra & experiments | `obs-infra` | Workloads, SAM template, experiment runner, idle-gap scheduler |
| B: Data pipeline & dashboard | `obs-pipeline` | Collector, DynamoDB, CloudWatch metrics, Grafana |
| C: ML & analysis | `obs-ml` | Pricing, cost meter, models, SHAP, recommender |

Anjali sends you your temporary password privately.

## 1. Sign in to the console

1. Open **https://402545554695.signin.aws.amazon.com/console**.
   Don't use the normal "Sign in to AWS" page with an email address; that is
   for account owners only.
2. Account ID: `402545554695` (already filled in by the link).
3. Enter your user name and temporary password.
4. Choose a new password when asked.
5. In the region menu (top right), select **Europe (Stockholm) eu-north-1**.
   Project resources only appear in this region.

## 2. Turn on MFA (required)

1. Click your user name (top right), then **Security credentials**.
2. Under **Multi-factor authentication (MFA)**, choose **Assign MFA device**,
   then **Authenticator app**.
3. Scan the QR code with Google Authenticator, Microsoft Authenticator or similar,
   and enter two codes in a row.
4. Sign out and back in to check it works.

## 3. Set up command-line access

You need this to deploy, run experiments and read data.

1. Install:
   - [AWS CLI v2](https://aws.amazon.com/cli/)
   - [AWS SAM CLI](https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/install-sam-cli.html) (roles A and B)
   - Python 3.11 or newer, then `pip install boto3 pyyaml`
   - [Docker Desktop](https://www.docker.com/products/docker-desktop/) (roles A and B, for `sam build`)
2. In the console: your user name → **Security credentials** → **Create access key**
   → **Command Line Interface (CLI)**. Copy both keys. The secret is shown only once.
3. On your laptop, run:

   ```
   aws configure
   ```

   and enter:

   ```
   AWS Access Key ID:     <your key id>
   AWS Secret Access Key: <your secret key>
   Default region name:   eu-north-1
   Default output format: json
   ```

4. Check it works:

   ```
   aws sts get-caller-identity
   ```

   It should show `"Arn": "arn:aws:iam::402545554695:user/<your user name>"`.
   From the repo folder, `python scripts/check_account.py` also lists the
   account settings.

If you already use AWS for something else, keep this login separate:
`aws configure --profile observatory`, then add `--profile observatory` to
commands or set `AWS_PROFILE=observatory`.

## What you can and can't do

All three logins have the same project permissions.

**You can:**
- Deploy, update and delete the `observatory` stack (`sam build`, `sam deploy`)
- Create, invoke and configure Lambda functions; run `runner/force_cold.py`
- Read and write the DynamoDB tables `invocations`, `import_profiles`, `recommendations`
- View logs and CloudWatch metrics, and create dashboards and alarms
- Create EventBridge schedules
- Look up Lambda prices (Price List API) and view the budget
- Manage your own password, MFA and access keys

**You can't:**
- Create or change other IAM users or permissions
- Use services the project doesn't need (EC2, RDS and so on)
- Work with project services outside the eu-north-1 region
- Change billing or the budget

If something fails with `AccessDenied` and you think the project needs it,
ask Anjali rather than working around it.

## Rules

- **Never share or commit access keys.** Keys belong in `~/.aws/credentials`
  only. If a key leaks, deactivate it right away under Security credentials
  and create a new one.
- **Name new stacks and roles starting with `observatory`.** Your permissions
  only cover that prefix, so other names fail to deploy.
- **Provisioned concurrency and SnapStart cost money by the hour.** Turn them
  on only for a test run, turn them off right after, and say so in the group chat.
- **The budget is $5/month.** Alerts go to Anjali at 50% and 100%.
- Code goes through GitHub: one branch per task, pull request to `main`.

## Grafana

The dashboard runs on Grafana Cloud. B sends invites. Grafana reads CloudWatch
through a separate read-only user (`observatory-grafana`); don't use your own
keys in Grafana.
