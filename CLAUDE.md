# Serverless Cold-Start Observatory

Course project. Measures, explains and costs AWS Lambda cold starts, then
recommends the cheapest strategy (on-demand, SnapStart, provisioned
concurrency) that meets a latency target per function.

Full plan: `docs/TASKS.md`. Proposal: `docs/proposal.pdf`. Setup, team access and usage: `README.md`.

## Roles
- **A (Infra & experiments):** workloads, SAM, experiment runner, idle-gap scheduler
- **B (Data pipeline & dashboard):** collector, DynamoDB, metrics, Grafana
- **C (ML & analysis):** pricing, cost meter, models, SHAP, recommender, report figures

Keep the role split in docs and handovers.

## Status (2026-10-19, end of day)
- Roles A and B (Anjali) are complete. ML (role C, teammate) starts from `docs/HANDOVER_ML.md`.
- Deployed: stack `observatory` with `--matrix full` (44 test functions + collector + idle-gap Lambda).
- Data: `core-sweep-20261019` (44 functions x 30 cold starts), `snapstart-20261019` (web/data-512-x86-zip,
  30 restores each), `idle-gap-20261019` (8 chains on tiny/web/data/ml-{512,1024}-arm64-zip, STILL RUNNING).
- Still to do: stop idle-gap chains (`python runner/idle_gap.py stop`) and re-export CSVs; final teardown
  (`sam delete`) only after C is done, since it deletes the tables.
- Provisioned concurrency never measured (quota 10, case 179237768000615); modelled from pricing instead.
- Keep `docs/TASKS.md` checkboxes and this section current when tasks finish.

## Team access
- One shared AWS account. Each teammate has their own IAM user (`obs-infra`, `obs-pipeline`, `obs-ml`)
  in group `observatory-developers` (project-scoped policy in `infra/account-setup.yaml`).
  Grafana uses read-only user `observatory-grafana` (CloudWatchReadOnlyAccess).
- Code via GitHub (one branch per task). Data via DynamoDB or CSV exports.

## Stack
- AWS Lambda (Python 3.12), AWS SAM, EventBridge, CloudWatch Logs + metrics, DynamoDB
- Python: boto3, pandas, xgboost (2.x), shap, scikit-learn, matplotlib
- Grafana (Docker locally or Grafana Cloud free tier) with the CloudWatch data source

## Repo layout
```
workloads/    tiny, web, data, ml handlers (app.py) + requirements.txt each
infra/        generate_template.py -> template.yaml (generated, gitignored), samconfig.toml,
              account-setup.yaml (team IAM users + Grafana read-only user)
collector/    log parser Lambda (app.py); cost.py + pricing.json are copied in from ml/ at generate time
runner/       force_cold.py; idle-gap scheduler Lambda (Stage 3)
ml/           pricing.json, cost.py, train.py, recommender.py, notebooks/
scripts/      check_account.py (read-only), create_budget.py
tests/        test_collector.py (plain asserts, no AWS)
dashboards/   exported Grafana JSON
docs/         TASKS.md, proposal.pdf, data_contract.md
```

## Environment
- AWS account 402545554695, region eu-north-1 (Stockholm), CLI default profile = IAM user `anjali-admin`.
- Windows. Use Bash/cmd; PowerShell works for the Claude tool but was flaky in the user's terminal.
  AWS CLI is at `C:\Program Files\Amazon\AWSCLIV2\aws.exe` and SAM at
  `C:\Program Files\Amazon\AWSSAMCLI\bin\sam.cmd` (shells opened before the install may not have them on PATH).
- Local Python is 3.11, not 3.12, so always build with `sam build` (samconfig sets `use_container = true`).
  Docker Desktop must be running. arm64 builds need the image pulled once with
  `docker pull --platform linux/arm64 public.ecr.aws/sam/build-python3.12:latest-arm64`.
- `sam deploy` and other resource-creating AWS commands are blocked for Claude in auto mode:
  prepare everything, then give the user the exact command to run.

## Conventions
- Function names: `{workload}-{memory}-{arch}-{packaging}`, e.g. `data-512-arm64-zip`;
  arch is `x86` or `arm64` in names (Lambda's `x86_64` only in the template).
- One SAM stack, `observatory`, holds tables, collector and all test functions.
  Grow the fleet by changing `MATRICES` in `infra/generate_template.py` (`pilot`, `core`), not by hand-editing the template.
- Stack and IAM role names start with `observatory`: the teammates' IAM policy only allows that prefix,
  so anything else breaks their deploys.
- Region and account come from the AWS profile / SAM config, never hardcoded.
- Every test function sets `PYTHONPROFILEIMPORTTIME=1`; log groups keep 7 days.
- Prices live only in `ml/pricing.json` (from the AWS Price List API for eu-north-1). Code reads them via `ml/cost.py`.
- Small PRs, one branch per task, never commit AWS credentials.

## Key technical decisions
- Cold start = REPORT line contains `Init Duration`. SnapStart shows `Restore Duration`
  (and `Billed Restore Duration`, which the parser must not confuse with it).
- Force a cold start by updating a dummy env var (`BUST`), waiting with the
  `function_updated_v2` waiter, then invoking. Merge with the existing env vars; the update replaces the whole map.
- The runner and collector both `UpdateItem` the same `invocations` key (`function_id`, `request_id`);
  the invoke response's RequestId equals the REPORT line's RequestId.
- Logs reach the collector via a CloudWatch Logs subscription filter with
  pattern `?REPORT ?"import time"`; payload is base64 + gzip.
- Import lines are joined to a cold start by log stream (one stream per execution environment).
  Only modules with cumulative time >= 1 ms are stored (`MIN_IMPORT_US`) to keep DynamoDB writes cheap.
  CloudWatch can split one cold start's import lines across batches, so the collector counts them per stream
  with `ADD` in `import_profiles` row `#stream {log_stream}`; `n_imports` is joined in at export, not stored on invocations.
- `scripts/demo_pipeline.py` runs fresh cold starts and prints every pipeline step (use it to verify changes and for the demo).
- INIT is billed at the normal duration price (since Aug 2025), so `init_cost_usd` = GB × init seconds × duration price.
- Collector writes DynamoDB rows and CloudWatch EMF metrics (namespace `ColdStartObservatory`).
  Keep EMF dimensions to workload, arch, packaging (each combination is a billed metric).
- SnapStart doesn't support container images or provisioned concurrency: strategy experiments use ZIP.
- ML validation uses GroupKFold grouped by `function_id` (no leakage).
- Init-time model uses XGBoost quantile regression (alpha 0.1 / 0.5 / 0.9).
- Recommender is rule-based on top of model predictions + pricing.json.

## Data contract (DynamoDB)
Full schema in `docs/data_contract.md`; change it there first.
- `invocations`: PK `function_id`, SK `request_id`. Fields: ts, workload, memory_mb,
  arch, packaging, package_mb, n_imports (joined at export), log_stream, idle_gap_min, is_cold, init_ms,
  restore_ms, billed_restore_ms, duration_ms, billed_ms, max_mem_mb, init_cost_usd,
  strategy, run_id, forced
- `import_profiles`: PK `function_id`, SK `module`. Fields: self_us, cumulative_us, log_stream, ts
- `recommendations`: PK `function_id`. Fields: strategy, pred_init_p50_ms,
  pred_init_p90_ms, monthly_cost_usd, p95_ms, latency_target_ms, traffic_rps, generated_at

## Cost safety
- Budget: $5/month via `scripts/create_budget.py` (check it exists with `scripts/check_account.py`).
- Provisioned concurrency and SnapStart cost money by the hour:
  enable only during a test run and remove right after.
- Before ending a session that deployed anything paid, list what is still running.
