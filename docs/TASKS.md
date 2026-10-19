# Tasks and handovers

Roles:
- **A (Infra & experiments):** workloads, SAM, experiment runner, idle-gap scheduler
- **B (Data pipeline & dashboard):** collector, DynamoDB, metrics, Grafana
- **C (ML & analysis):** pricing, cost meter, models, SHAP, recommender, report figures

Rule: B and C never wait on A. C works on mock data until real data arrives.

---

## Stage 0: Setup (all, ~3 days)
- [ ] One AWS account, budget alert ($5), IAM users per person (least privilege)
  - Account + $5 budget done. Teammate IAM users: template ready (`infra/account-setup.yaml`), not deployed yet.
- [ ] GitHub repo with the layout from CLAUDE.md, branch protection on main
  - Local repo + layout done; GitHub repo not created yet.
- [ ] Agree on `docs/data_contract.md` (table schemas from CLAUDE.md)
  - Draft done (SK changed to `request_id`); waiting for team sign-off.
- [x] Check Lambda concurrency quota and SnapStart (Python) availability in our region
  - Quota is 10; increase to 1000 requested 2026-10-19 (pending). SnapStart for Python: available in eu-north-1.

**Handover:** data contract signed off by all three.

---

## Stage 1: Pilot end to end (week 1–2)
**A**
- [x] Write `tiny`, `web`, `data` workloads (handler returns `{"ok": true}`)
- [x] `infra/generate_template.py` for a 3-function pilot, `sam deploy`
- [x] `runner/force_cold.py`: force cold start + invoke, log metadata to `invocations`

**B**
- [x] DynamoDB tables in the SAM template
- [x] Collector Lambda: decode payload, parse REPORT + import-time lines, write rows
- [x] Subscription filter on pilot log groups

**C**
- [x] `ml/pricing.json` for our region (x86, arm64, requests, PC, SnapStart)
- [x] `ml/cost.py`: init cost per invocation, monthly projection
- [ ] Generate a mock dataset matching the contract; start EDA notebook

Pilot verified 2026-10-19: 6 forced cold starts (tiny/web/data, 512 MB, x86) landed as merged rows with `init_ms` and import profiles.

**Handovers:** A → B: pilot function names and log groups.
B → C: first real CSV export from `invocations`. Pilot works when one forced cold
start shows up as a row with `init_ms` and an import profile.

---

## Stage 2: Full experiments + first dashboard (weeks 3–4)
**A**
- [ ] Expand matrix (core sweep: workloads × memory × arch, ZIP)
- [ ] Packaging experiment: `data` as ZIP vs layer vs container, both archs
- [ ] Parallel runner (8 threads) + concurrent-burst trick; ~30 cold samples each
- [x] `scripts/export_csv.py` run after every experiment

**B**
- [x] EMF metrics from collector (InitDurationMs, ColdStart, ColdStartCostUSD)
  - Built into the collector in Stage 1; verified in CloudWatch for the pilot.
- [x] Grafana panels: cold-start rate, init vs memory by arch, init by packaging,
      cost per function, top slow imports
  - Local Grafana via `dashboards/docker-compose.yml` (read-only `observatory-grafana` profile); dashboard JSON built by `dashboards/build_dashboard.py`.

**C**
- [ ] Feature table from real data; baseline model on core sweep
- [ ] arm64 vs x86 and packaging comparison figures

**Handover:** A → C: full core-sweep CSV. B → all: dashboard link.

---

## Stage 3: Idle-gap tests + models (weeks 5–6)
**A**
- [ ] Idle-gap scheduler Lambda on EventBridge (gaps 1, 5, 10, 20, 40 min), run 2–4 days
- [ ] Strategy experiment: `web` and `data` under on-demand, SnapStart, PC (short runs)

**B**
- [ ] Model accuracy panel (predicted vs actual init time)
- [ ] Panel for strategy results

**C**
- [ ] Model A: XGBoost quantile regression for init time, GroupKFold, MAE/MAPE, coverage
- [ ] Model B: cold-start probability classifier, ROC-AUC, calibration plot
- [ ] SHAP summary plot

**Handover:** A → C: idle-gap and strategy CSVs. A tears down PC/SnapStart.

---

## Stage 4: Recommender + finish (weeks 7–8)
**C**
- [ ] `ml/recommender.py`: cheapest strategy meeting latency target per function
- [ ] Write results to `recommendations`

**B**
- [ ] Recommendation panel; export dashboard JSON to `dashboards/`
- [ ] (Optional) what-if panel

**A**
- [ ] Final teardown of paid resources; README with deploy steps

**All**
- [ ] Report: problem, method, results, limitations
- [ ] Demo recording
