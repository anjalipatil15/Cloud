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

The infrastructure and data-pipeline side (roles A and B) is complete.
The ML side (role C) starts from [docs/HANDOVER_ML.md](docs/HANDOVER_ML.md).

| Stage | Done | Open |
|---|---|---|
| 0 Setup | AWS account (eu-north-1), $5 budget, teammate IAM users, GitHub repo, data contract | Lambda quota increase (pending with AWS) |
| 1 Pilot | Workloads, template generator, collector, runner, pricing, cost model | C: mock dataset / EDA (superseded by real data) |
| 2 Experiments | 44 functions deployed; 30 forced cold starts each; packaging experiment; CSV export; Grafana dashboard | C: feature table, comparison figures |
| 3 Experiments | Idle-gap chains on 8 functions; SnapStart experiment on `web` and `data` | Provisioned concurrency (blocked by quota; modelled instead); C: models, SHAP |
| 4 Finish | Recommendation panel wired up; cost check script; teardown steps | C: recommender; report; demo |

Item-level status: [docs/TASKS.md](docs/TASKS.md).

### Results so far

Median cold-start (INIT) time from the core sweep:

| Workload | x86 | arm64 |
|---|---|---|
| tiny (no imports) | ~78 ms | ~68 ms |
| web (requests, jinja2, pydantic) | ~500 ms | ~445 ms |
| data (pandas) | ~2,050 ms | ~1,785 ms |
| ml (scikit-learn) | ~3,500–4,100 ms | ~3,450 ms |

- **Memory size barely changes init time**, from 256 MB to 2 GB.
- **arm64 starts 10–15% faster** and costs about 20% less per GB-second.
- **Container images start about 3× faster than ZIP for `data` on x86** (~700 ms vs ~2,080 ms). Layers make no difference compared with ZIP.
- **SnapStart cuts `data` from ~2,080 ms to ~560 ms**, but doesn't help `web`, which already starts in ~500 ms.
- **Importing pandas takes 1.3–1.6 s**, most of `data`'s cold start.

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
| `runner/` | Cold-start forcer, idle-gap scheduler Lambda, SnapStart / provisioned-concurrency experiment |
| `ml/` | `pricing.json`, `cost.py`; models and recommender come later |
| `scripts/` | Account check, budget, CSV export, demo, publish recommendations, what's-still-billed check |
| `tests/` | Unit tests (no AWS needed) |
| `dashboards/` | Grafana: docker-compose, provisioning, dashboard JSON and its builder |

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
python infra\generate_template.py --matrix full
cd infra
sam build
sam deploy
```

- `--matrix full` = 36 core functions + 8 packaging variants (what is deployed now); `pilot` = 3 functions, `core` = 36.
- One stack holds everything, so a deploy with a smaller matrix deletes the functions it leaves out.
- `sam deploy` shows a change set and asks for confirmation.
- Building arm64 functions on an x86 PC needs the arm64 build image, pulled once:
  `docker pull --platform linux/arm64 public.ecr.aws/sam/build-python3.12:latest-arm64`

## Run an experiment

| Experiment | Command |
|---|---|
| Forced cold starts | `python runnerorce_cold.py --all --samples 30 --threads 6` |
| Idle-gap chains (cold-start probability) | `python runner\idle_gap.py start --functions web-512-arm64-zip ...`, then `status` / `stop` |
| SnapStart | `python runner\strategy.py snapstart --functions data-512-x86-zip --samples 30 --burst 1` |
| Provisioned concurrency | `python runner\strategy.py provisioned --functions data-512-x86-zip` (needs quota > 10) |
| End-to-end check / demo | `python scripts\demo_pipeline.py` |

Results land in the DynamoDB `invocations` table within about a minute (log
delivery delay). Then run `python scripts\export_csv.py` to write `data/*.csv`,
and commit those files: `data/` is in git so the ML side can use it without AWS access.

Keep thread counts so that the total concurrent invocations stay under the
account's concurrency limit (currently 10).

## Dashboard

```cmd
docker compose -f dashboards/docker-compose.yml up -d
```

Open http://localhost:3000. Grafana reads CloudWatch with the read-only
`observatory-grafana` AWS profile. Panels are defined in
`dashboards/build_dashboard.py`; run it to regenerate `observatory.json`. ML
recommendations appear after `python scripts\publish_recommendations.py`.

## Tests

```cmd
python tests\test_collector.py
```

## Cost safety

- `python scripts\check_running.py` lists everything still billed while idle:
  provisioned concurrency, SnapStart, published versions, idle-gap schedules and ECR images.
  Run it at the end of every session that ran an experiment.
- Idle ZIP functions, tables and log groups cost almost nothing. The whole
  measurement campaign cost well under $1.
- **The Grafana dashboard costs money per view.** Its Logs Insights panels are billed
  per GB scanned (first 5 GB/month free, then about $0.005/GB). One load reads about 1 GB
  of logs. Auto-refresh is off for this reason: don't turn it on, and close the
  dashboard tab when you're done.
- **Provisioned concurrency and SnapStart charge by the hour.** `runner/strategy.py`
  removes them when a run ends; `python runner\strategy.py cleanup --functions ...` removes leftovers.

## Teardown

When the project is finished (after the ML side has exported what it needs):

```cmd
python runner\idle_gap.py stop
python runner\strategy.py cleanup --functions web-512-x86-zip data-512-x86-zip
python scripts\export_csv.py
cd infra
sam delete --stack-name observatory
```

`sam delete` removes the functions, **the DynamoDB tables with all data**, the
log groups and the ECR images, so export the CSVs first. The budget and the
`observatory-account` stack (team IAM users) are separate; delete them in the console if needed.
