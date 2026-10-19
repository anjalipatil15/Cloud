# Serverless Cold-Start Observatory

Course project (3 people). Measures, explains and costs AWS Lambda cold starts,
then recommends the cheapest strategy (on-demand, SnapStart, provisioned
concurrency) that meets a latency target per function.

Full plan: `docs/TASKS.md`. Proposal: `docs/proposal.pdf`.

## Stack
- AWS Lambda (Python 3.12), AWS SAM, EventBridge, CloudWatch Logs + metrics, DynamoDB
- Python: boto3, pandas, xgboost (2.x), shap, scikit-learn, matplotlib
- Grafana (Docker locally or Grafana Cloud free tier) with the CloudWatch data source

## Repo layout
```
workloads/    tiny, web, data, ml handlers + requirements.txt each
infra/        generate_template.py -> template.yaml (SAM)
collector/    log parser Lambda (REPORT + import-time lines)
runner/       cold-start forcer, idle-gap scheduler Lambda
ml/           pricing.json, cost.py, train.py, recommender.py, notebooks/
dashboards/   exported Grafana JSON
docs/         TASKS.md, proposal.md, data_contract.md
```

## Conventions
- Function names: `{workload}-{memory}-{arch}-{packaging}`, e.g. `data-512-arm64-zip`
- Region and account come from env vars / SAM config, never hardcoded
- Every test function sets `PYTHONPROFILEIMPORTTIME=1`
- Prices live only in `ml/pricing.json` (fill from the AWS pricing page for our region)
- Small PRs, one branch per task, never commit AWS credentials

## Key technical decisions
- Cold start = REPORT line contains `Init Duration`. SnapStart shows `Restore Duration`.
- Force a cold start by updating a dummy env var (`BUST`), waiting with the
  `function_updated_v2` waiter, then invoking.
- Logs reach the collector via a CloudWatch Logs subscription filter with
  pattern `?REPORT ?"import time"`; payload is base64 + gzip.
- Collector writes DynamoDB rows and CloudWatch EMF metrics. Keep EMF
  dimensions to workload, arch, packaging (each combination is a billed metric).
- ML validation uses GroupKFold grouped by `function_id` (no leakage).
- Init-time model uses XGBoost quantile regression (alpha 0.1 / 0.5 / 0.9).
- Recommender is rule-based on top of model predictions + pricing.json.

## Data contract (DynamoDB)
- `invocations`: PK `function_id`, SK `request_id`. Fields: workload, memory_mb,
  arch, packaging, package_mb, n_imports, idle_gap_min, is_cold, init_ms,
  restore_ms, duration_ms, billed_ms, max_mem_mb, init_cost_usd, strategy
- `import_profiles`: PK `function_id`, SK `module`. Fields: self_us, cumulative_us
- `recommendations`: PK `function_id`. Fields: strategy, pred_init_p50_ms,
  pred_init_p90_ms, monthly_cost_usd, p95_ms, latency_target_ms
Change these only by updating `docs/data_contract.md` and telling the team.

## Cost safety
- AWS Budgets alert is set. Provisioned concurrency and SnapStart cost money:
  enable only during a test run and remove right after.
- Before ending a session that deployed anything paid, list what is still running.
