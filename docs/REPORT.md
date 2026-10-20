# Serverless Cold-Start Observatory: report draft

> **Status:** real-data analysis rerun on the GitHub `data/` exports (verified
> byte-for-byte against the local CSVs on 2026-10-20). Model metrics and
> recommendation counts below are from those exports, not the mock dataset.
> Recommendations remain modelled estimates; see the Model B extrapolation
> limitation in §4 before interpreting them operationally.

## 1. Problem

AWS Lambda creates a new execution environment when no warm one is free. That
**cold start** adds an INIT phase (runtime start + module imports + handler-module
code) before the request runs. Since Aug 2025 INIT is billed for ZIP functions on
managed runtimes too. Teams can reduce cold starts in three ways:

| Strategy | Latency effect | Cost effect |
|---|---|---|
| On-demand (do nothing) | cold invokes pay full INIT | pay INIT per cold start |
| SnapStart | cold invokes pay a snapshot *restore* instead of INIT | snapshot cache (per GB-s, all month) + per-restore fee |
| Provisioned concurrency (PC) | no cold starts up to the provisioned level | pay per GB-s for kept-warm environments, all month |

**Question:** for a given function, latency target and traffic level, which
strategy is cheapest while still meeting the target, and what drives INIT time?

## 2. Method

### 2.1 Measurement (roles A, B)
- 44 test functions in eu-north-1, Python 3.12: 4 workloads (`tiny`: no imports;
  `web`: requests + jinja2 + pydantic; `data`: pandas; `ml`: scikit-learn) ×
  memory 128–2048 MB × x86/arm64, ZIP. There are also 8 packaging variants of
  `data` (layer, container image).
- **Forced cold starts:** changing an environment variable drops the warm
  environments. 30 cold starts per function (`core-sweep-20261019`).
- **Idle-gap chains:** 8 arm64 functions are called after random 1–40 min gaps for
  about a day (`idle-gap-20261019`), which gives P(cold | gap).
- **SnapStart:** 30 restores each on `web-512-x86-zip` and `data-512-x86-zip`.
- A collector Lambda parses REPORT lines and `PYTHONPROFILEIMPORTTIME` output into
  DynamoDB. The data is exported to CSV (`scripts/export_csv.py`).

### 2.2 Cost model (`ml/cost.py`, `ml/pricing.json`)
Prices come from the AWS Price List API for eu-north-1. A monthly projection per
strategy adds up requests, duration, INIT (on-demand), snapshot cache + restores
(SnapStart) and kept-warm GB-s (PC).

### 2.3 Model A: INIT time (`ml/model_init.py`)
- Target `log(init_ms)` on cold rows of the core sweep (the one warm row is dropped).
  Features: workload, memory_mb, arch, packaging, package_mb, n_imports.
- XGBoost `reg:quantileerror` at α = 0.5 and α = 0.9. If the two quantile
  predictions cross, p90 is raised to p50.
- **GroupKFold (5) by `function_id`.** The 30 samples of one function are
  near-identical, so row-level CV would leak and score the model on
  configurations it has already seen.
- Baseline: median INIT per (workload, arch) from the training folds.
- Metrics: per-function MAE and MAPE (p50), empirical coverage of p90, and pinball
  loss. SHAP (TreeExplainer) on the p50 model.

### 2.4 Model B: cold-start probability (`ml/model_cold.py`)
- Rows from the idle-gap run that have a gap (the first invoke of each chain is dropped).
- Logistic regression on idle_gap_min + workload + log2(memory), compared with a
  gap-only logistic model and an XGBoost classifier. GroupKFold by function.
  Metrics: ROC-AUC, Brier score, calibration curve.
- **From probability to rate:** with Poisson traffic at *r* req/s the gaps are
  exponential with mean 1/r, so cold rate = E[P(cold | G)].

### 2.5 Recommender (`ml/recommender.py`)
For each function and each scenario (p95 target ∈ {500, 1000} ms, traffic ∈
{0.001, 0.01, 0.1, 1} req/s):
- **Latency:** warm, cold and restore latencies are each modelled as lognormals
  fitted to measured p50/p90 (p95 for handler duration). The expected p95 is the
  0.95-quantile of the mixture (1 − c)·warm + c·cold.
- **On-demand:** c from Model B; cold = INIT + handler.
- **SnapStart:** cold = restore + handler. Measured restores are used for
  web/data-512-x86. For other functions the restore is estimated as
  max(fastest measured restore, ratio × INIT), with the ratio taken from `data`
  (~0.27). SnapStart isn't offered for container images, because Lambda doesn't support it.
- **Provisioned:** *modelled only*. It has no cold starts, p95 is the warm p95, and
  the concurrency is ⌈1.5 × r × mean duration⌉ (at least 1).
- The recommender picks the cheapest strategy that meets the target; if none does,
  it picks the lowest p95. One scenario (default 1000 ms, 0.1 req/s) goes to the
  DynamoDB `recommendations` table and from there to Grafana.

## 3. Results

### 3.1 What drives INIT (measured, from the handover)

| Workload | x86 | arm64 |
|---|---|---|
| tiny | ~78 ms | ~68 ms |
| web | ~500 ms | ~445 ms |
| data | ~2,050 ms | ~1,785 ms |
| ml | ~3,500–4,100 ms | ~3,450 ms |

- **Imports dominate.** pandas alone takes 1.3–1.6 s of `data`'s INIT
  (`fig_imports.png`).
- **Memory barely matters** (`fig_memory.png`): `data` x86 takes 1,908 ms at 256 MB
  and 2,021 ms at 2048 MB. More memory makes the *handler* faster, but not INIT.
- **arm64 is 10–15% faster** to start and ~20% cheaper per GB-s (`fig_arch.png`).
- **Packaging:** container images start about 3× faster than ZIP for `data` on x86
  (~700 vs ~2,080 ms). Layers are the same as ZIP (`fig_packaging.png`).
- **SnapStart** cuts `data` from ~2,080 ms to ~560 ms but makes `web` slightly
  worse (~585 vs ~510 ms): a restore has a floor of about 0.5 s
  (`fig_snapstart.png`).

### 3.2 Model A (real data)
| | MAE per function | MAPE per function |
|---|---|---|
| Baseline (median by workload × arch) | 205 ms | 21.9 % |
| XGBoost p50 | 76 ms | 4.4 % |

p90 coverage: 0.914 (target 0.90). SHAP ranks `n_imports` and `package_mb` first,
then packaging and architecture, with **memory last** (`fig_shap_summary.png`). That
matches §3.1: what is imported matters, and memory doesn't. `package_mb`,
`n_imports` and workload are strongly correlated, so SHAP divides the credit
among them. Read them together as "how much code gets loaded".

### 3.3 Model B (real data)
On 450 idle-gap invokes across 8 functions, GroupKFold ROC-AUC was 0.835 for
logistic regression, 0.883 for XGBoost, and 0.878 for the gap-only logistic
model. Their Brier scores were 0.131, 0.101 and 0.095, respectively
(`fig_model_b_calibration.png`). The fitted full logistic model reaches P(cold) =
0.5 at gaps of 0.66–2.74 minutes, depending on workload (`model_b_metrics.json`);
the gap-only model performs comparably to the full logistic model.

### 3.4 Recommendations (real-data inputs)
Number of the 44 functions assigned to each strategy by the model:

| p95 target | req/s | on-demand | SnapStart | provisioned |
|---|---|---|---|---|
| 500 ms | 0.001 | 14 | 0 | 30 |
| 500 ms | 0.01 | 14 | 0 | 30 |
| 500 ms | 0.1 | 14 | 0 | 30 |
| 500 ms | 1.0 | 14 | 0 | 30 |
| 1000 ms | 0.001 | 23 | 12 | 9 |
| 1000 ms | 0.01 | 23 | 13 | 8 |
| 1000 ms | 0.1 | 23 | 0 | 21 |
| 1000 ms | 1.0 | 22 | 0 | 22 |

At the default 1000 ms, 0.1 req/s scenario, the model selects on-demand for 23
functions and provisioned concurrency for 21; one function does not meet the
target under any option. SnapStart is selected for 12–13 functions at the lower
traffic scenarios, but none at 0.1 req/s or above. For the 500 ms target, the
model selects provisioned concurrency for 30 functions at every tested traffic
level. The full per-function results and option grid are in
`ml/notebooks/recommendations.csv` and `ml/notebooks/recommendations_grid.csv`.

These are scenario outputs, not measured strategy outcomes. In particular,
Model B was fit on idle gaps from about 1 to 40 minutes, but the recommender
integrates its logistic curve over exponential gaps that can be shorter than
the observed range. The fitted curve has a nonzero cold probability at zero
gap, so this extrapolation can overstate cold-start rates at high traffic and
affect which strategy is selected. Provisioned concurrency is also modelled,
not measured. Validate the short-gap behaviour with additional measurements
before using these strategy selections as operational advice.

## 4. Limitations
- **Provisioned concurrency was not measured.** The account concurrency quota is 10
  and Lambda keeps 10 unreserved. PC is modelled from pricing: no cold starts,
  warm latency.
- **SnapStart was measured on 2 functions only.** Restores for the others are
  extrapolated with a floor-plus-ratio rule.
- **Idle-gap ran ~1 day instead of 2–4,** so there are fewer samples at long gaps,
  and only for arm64 at 512/1024 MB.
- **Cold-rate model:** Poisson arrivals, one concurrent request at a time.
  Scale-out cold starts (bursts) and the periodic recycling of environments are
  not modelled. Also, the fitted logistic model is extrapolated below the
  measured idle-gap range (about 1–40 minutes); its nonzero zero-gap probability
  may overestimate cold starts at high traffic. `recommender.py --cold-floor`
  only adds a minimum and does not correct that extrapolation.
- `data`/`ml` have no 128 MB variant, because pandas needs ~130 MB just to import.
- One region (eu-north-1), one runtime (Python 3.12), synthetic handlers.
- Model A is trained on 44 configurations, so it interpolates within this design
  and doesn't generalise to arbitrary code.

## 5. Reproduce
See README → "ML analysis (role C)". Every figure referenced here is in `ml/notebooks/`.
