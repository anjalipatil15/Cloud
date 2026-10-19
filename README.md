# Serverless Cold-Start Observatory

Measures, explains and costs AWS Lambda cold starts, then recommends the
cheapest strategy (on-demand, SnapStart or provisioned concurrency) that meets
a latency target for each function.

A fleet of test Lambda functions is cold-started on purpose. A collector
Lambda parses their logs into DynamoDB and CloudWatch metrics. An XGBoost
model and a rule-based recommender then turn that data into per-function advice,
shown in Grafana.

Proposal: [docs/proposal.pdf](docs/proposal.pdf) · Plan: [docs/TASKS.md](docs/TASKS.md) ·
Data schemas: [docs/data_contract.md](docs/data_contract.md)

## Team and roles

| Role | Owns |
|---|---|
| **A: Infra & experiments** | Workloads, SAM template, experiment runner, idle-gap scheduler |
| **B: Data pipeline & dashboard** | Collector, DynamoDB, CloudWatch metrics, Grafana |
| **C: ML & analysis** | Pricing, cost meter, models, SHAP, recommender, report figures |

B and C never wait on A: C works on mock data until real data arrives.

## Progress (as of 2026-10-19)

The pilot works end to end. Six forced cold starts across `tiny`, `web` and
`data` (512 MB, x86, ZIP) arrived in DynamoDB as complete rows with init time,
cost and import profile. First results: init takes about 70 ms for `tiny`,
about 500 ms for `web`, and about 2,040 ms for `data`, where importing pandas
alone takes 1.57 s.

| Stage | Done | Open |
|---|---|---|
| 0 Setup | AWS account (eu-north-1), $5 budget, quota and SnapStart check, repo layout, data contract draft | Teammate IAM users (template ready), GitHub repo, data contract sign-off, Lambda quota increase (requested, pending) |
| 1 Pilot (A) | Workloads `tiny`/`web`/`data`/`ml`, template generator, pilot deployed, cold-start runner | — |
| 1 Pilot (B) | DynamoDB tables, collector Lambda (with tests), subscription filters | — |
| 1 Pilot (C) | `pricing.json` (from the AWS Price List API), `cost.py` | Mock dataset, EDA notebook |
| 2 Experiments | EMF metrics from the collector | Core sweep, packaging experiment, CSV export, Grafana |
| 3–4 | — | Idle-gap tests, models, recommender, report |

Item-level status: [docs/TASKS.md](docs/TASKS.md).

## Team access

Teammates: follow **[docs/TEAM_ACCESS.md](docs/TEAM_ACCESS.md)**.

Everyone works in one shared AWS account (region `eu-north-1`), each with their own login.

| What | How each person gets in | Set up by |
|---|---|---|
| **AWS console + CLI** | Own IAM user (`obs-infra`, `obs-pipeline`, `obs-ml`), all in the `observatory-developers` group | Account admin, once |
| **Code** | GitHub repo, added as a collaborator; one branch per task, PR to `main` | Repo owner |
| **Data** | CLI access to the DynamoDB tables, or CSV exports (`scripts/export_csv.py`, Stage 2) | — |
| **Dashboard** | Grafana Cloud invite. Grafana reads CloudWatch through the read-only `observatory-grafana` user | B |

The `observatory-developers` permissions are limited to this project in this region:

- **Can:** deploy and delete the `observatory` stack; manage Lambda, DynamoDB, logs, EventBridge and CloudWatch; create roles whose names start with `observatory`.
- **Can't:** change other IAM users or policies, or use services outside the project.
- **Self-service:** each person manages their own password, MFA and access keys.

**Admin, once:**

```cmd
aws cloudformation deploy --template-file infra\account-setup.yaml --stack-name observatory-account --capabilities CAPABILITY_NAMED_IAM
```

Then, for each person:

1. Go to IAM → Users → the user → **Enable console access**, and set a temporary password.
2. Send them the sign-in URL (`https://<account-id>.signin.aws.amazon.com/console`), their user name and the password.

**Each teammate:**

1. Sign in, set a new password and turn on MFA.
2. Create an access key (Security credentials → Create access key → CLI).
3. Install the prerequisites below, then run `aws configure` with region `eu-north-1`.
4. Run `python scripts\check_account.py` to confirm access.

Never share access keys or commit them. Each person uses their own.

## How it works

```
runner/force_cold.py ──update BUST env var──▶ test functions (tiny / web / data / ml)
        │                                            │ REPORT + "import time" log lines
        │ run_id, strategy, package_mb               ▼
        │                                  CloudWatch Logs ──subscription filter──▶ collector Lambda
        ▼                                                                            │
   DynamoDB `invocations` ◀──────────── init_ms, duration, cost, is_cold ───────────┤
   DynamoDB `import_profiles` ◀──────── slow modules per cold start ────────────────┤
   CloudWatch metrics (ColdStartObservatory) ◀── EMF ───────────────────────────────┘
        │
        ▼
   ml/ (cost model, XGBoost, recommender) ──▶ `recommendations` ──▶ Grafana
```

- **Cold start:** the REPORT line contains `Init Duration` (or `Restore Duration` with SnapStart).
- **Forcing one:** changing any environment variable makes Lambda discard warm
  environments, so the next invoke starts cold.
- **Import profile:** every test function sets `PYTHONPROFILEIMPORTTIME=1`, so
  Python logs how long each module takes to import.

## Repo layout

| Path | What it is |
|---|---|
| `workloads/` | Test function handlers: `tiny` (no imports), `web` (requests, pydantic, jinja2), `data` (pandas), `ml` (scikit-learn) |
| `infra/generate_template.py` | Writes `infra/template.yaml` (SAM) for a function matrix |
| `infra/account-setup.yaml` | IAM users for the team and Grafana (see Team access) |
| `collector/` | Log-parser Lambda |
| `runner/` | Cold-start forcer (idle-gap scheduler comes in Stage 3) |
| `ml/` | `pricing.json`, `cost.py`; models and recommender come later |
| `scripts/` | Account check and budget setup |
| `tests/` | Unit tests (no AWS needed) |
| `dashboards/` | Exported Grafana JSON |

Test functions are named `{workload}-{memory}-{arch}-{packaging}`, for example `data-512-arm64-zip`.

## Prerequisites

- Your own IAM user for the team account (see Team access), set up with `aws configure`, region `eu-north-1`
- [AWS CLI v2](https://aws.amazon.com/cli/) and [AWS SAM CLI](https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/install-sam-cli.html)
- Python 3.11+ with `boto3` and `pyyaml` (`pip install boto3 pyyaml`)
- Docker Desktop, running (SAM builds the Linux packages inside containers)

## Account setup (admin, once; already done)

```cmd
python scripts\check_account.py
python scripts\create_budget.py you@example.com
```

`check_account.py` makes no changes. It shows the Lambda concurrency limit;
new accounts often get 10, and the experiments need more, so request 1000 in
Service Quotas (quota `L-B99A9384`). `create_budget.py` creates a $5/month
budget that emails you at 50% and 100% spend and at 100% forecast.

## Deploy

```cmd
python infra\generate_template.py --matrix pilot
cd infra
sam build
sam deploy
```

- `--matrix pilot` deploys 3 functions; `--matrix core` deploys the full 80-function sweep.
- `sam deploy` shows a change set and asks for confirmation.
- Building arm64 functions on an x86 PC needs the arm64 build image, pulled once:
  `docker pull --platform linux/arm64 public.ecr.aws/sam/build-python3.12:latest-arm64`

## Run an experiment

```cmd
python runner\force_cold.py --functions tiny-512-x86-zip data-512-x86-zip --samples 3
python runner\force_cold.py --all --samples 30 --threads 8
```

Each sample forces one cold start. Results land in the DynamoDB `invocations`
table within about a minute (log delivery delay).

## Tests

```cmd
python tests\test_collector.py
```

## Cost safety

- Idle functions, tables and log groups cost almost nothing. A full core sweep
  (80 functions × 30 cold starts) costs well under $1.
- **Provisioned concurrency and SnapStart charge by the hour.** Enable them only
  for a test run and remove them right after.
- Tear everything down with `sam delete --stack-name observatory` (run from `infra/`).
