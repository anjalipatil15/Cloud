# Handover to ML (role C)

The measurement side is finished. This page covers what data exists,
how to get it, what it already shows, and where your outputs plug back in.

Column definitions: [data_contract.md](data_contract.md). Plan: [TASKS.md](TASKS.md), Stages 3–4 for C.

## Get the data

The data is in the repo: **`data/invocations.csv`** and **`data/import_profiles.csv`**.
Pull `main` to get the latest export; no AWS access needed.

To export fresh copies from the live DynamoDB tables yourself, use your `obs-ml`
login (see [TEAM_ACCESS.md](TEAM_ACCESS.md)):

```
python scripts/export_csv.py        # overwrites data/invocations.csv and data/import_profiles.csv
```

## What's in `invocations.csv`

Filter on `run_id` to pick an experiment:

| run_id | What it is | Rows | Use it for |
|---|---|---|---|
| `core-sweep-20261019` | Forced cold starts, 30 per function, 44 functions: 36 core (4 workloads × 4–5 memory sizes × x86/arm64, ZIP) plus 8 packaging variants of `data` (layer and container image, 512/1024 MB, both archs). The two arm64 image functions were added later in the day after a build fix | ~1,320 | **Model A** (init time) |
| `check-arm64-image` | Smoke test of the fixed arm64 image | 2 | Ignore |
| `idle-gap-20261019` | Invokes after random idle gaps of 1–40 min on 8 arm64 functions (512 and 1024 MB) | grows ~30/hour | **Model B** (cold-start probability) |
| `snapstart-20261019` | `web-512-x86-zip` and `data-512-x86-zip` with SnapStart on (30 restores each, plus warm calls) | ~110 | Strategy comparison |
| `pilot-1`, `demo-*` | Early tests | ~15 | Ignore |

Useful columns:

- **Target for Model A:** `init_ms` on rows with `is_cold = True`.
- **Features for Model A:** `workload`, `memory_mb`, `arch`, `packaging`, `package_mb`, `n_imports`.
- **Model B:** `idle_gap_min` → `is_cold`. The first invoke of each chain has no `idle_gap_min`; drop it.
- **SnapStart:** `restore_ms` is set on restores; the other rows in that run are warm calls. `client_ms` is the end-to-end latency seen by the caller.
- **Cost:** `init_cost_usd` per cold start. Use `ml/cost.py` for monthly projections; prices are in `ml/pricing.json`.
- **Validation:** GroupKFold by `function_id`. Within one function the 30 samples are near-identical, so mixing them across folds leaks.

`import_profiles.csv` holds per-module import times (only modules ≥ 1 ms) for
each function: the data behind "which library makes it slow".

## What the data already shows

Medians from the core sweep (ms):

| Workload | x86 | arm64 | Note |
|---|---|---|---|
| tiny | ~78 | ~68 | runtime baseline |
| web | ~500 | ~445 | requests + jinja2 + pydantic |
| data | ~2,050 | ~1,785 | pandas alone is ~1.3–1.6 s of it |
| ml | ~3,500–4,100 | ~3,450 | scikit-learn |

1. **Memory barely changes init time.** For example, `data` x86 takes 1,908 ms at 256 MB and 2,021 ms at 2048 MB. Expect `memory_mb` to rank low in SHAP; that is a finding, not a bug.
2. **arm64 is 10–15% faster** for tiny/web/data, and about 20% cheaper per GB-second.
3. **Container images are about 3× faster for `data` on x86** (~700 ms vs ~2,080 ms for ZIP). Layers make no difference vs ZIP.
4. **SnapStart helps heavy functions only:** `data` restores in ~560 ms (p90 ~610) instead of ~2,080 ms; `web` restores in ~585 ms, slower than its ~510 ms init.

## Caveats to mention in the report

- **Provisioned concurrency was not measured.** The account's concurrency quota is 10, and Lambda keeps 10 unreserved, so none can be provisioned. Model it: no cold starts, latency = warm duration, cost from `pricing.json` (`monthly_cost_usd(strategy="provisioned", ...)`).
- **One core-sweep invocation came back warm** despite being forced. Filter on `is_cold`.
- **Idle-gap ran ~1 day, not 2–4.** Fewer samples at the 40-minute gap.
- **`data`/`ml` have no 128 MB variant:** pandas needs ~130 MB just to import.

## Where your outputs go

1. `ml/recommender.py` writes one row per function to the DynamoDB table
   `recommendations` (fields in [data_contract.md](data_contract.md): `strategy`,
   `pred_init_p50_ms`, `pred_init_p90_ms`, `monthly_cost_usd`, `p95_ms`,
   `latency_target_ms`, `traffic_rps`, `generated_at`).
2. Run `python scripts/publish_recommendations.py`. The Grafana panel
   **Strategy recommendations** then shows them.
3. Put report figures and notebooks in `ml/notebooks/`.

## Dashboard

Start it with `docker compose -f dashboards/docker-compose.yml up -d` (needs
Docker and the `observatory-grafana` AWS profile), then open http://localhost:3000.
To change panels, edit `dashboards/build_dashboard.py` and run it. Don't edit
`observatory.json` by hand.
