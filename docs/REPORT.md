# Serverless Cold-Start Observatory: report draft

> **Status (2026-10-20):** real-data analysis on the GitHub `data/` exports, not
> the mock dataset. Model B was revised on 2026-10-20 to model the gap as
> log(gap), so the predicted cold probability goes to 0 for very short gaps
> (§2.4). Model B metrics and all recommendation counts below are from that
> revised model. Recommendations are modelled estimates, not measured outcomes (§4).

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
- **Model used by the recommender:** logistic regression on **log(gap)** only,
  P(cold | g) = σ(a + b·log g). With b > 0 the curve rises monotonically and
  P(cold) → 0 as g → 0, which matches a function that is called every few
  seconds staying warm.
- Why log(gap): the idle gaps were only ever 1, 5, 10, 20 or 40 minutes. A
  logistic model that is linear in the gap extrapolates to a 26–44% cold
  probability at a zero-minute gap. The recommender integrates over gaps of a few
  seconds at 0.1 req/s, so that extrapolation drove most of its choices. Adding
  both a linear and a log term was also tried; it fitted a U-shape (P(cold) → 1 at
  0 min) and was rejected.
- Compared with: a logistic model on gap + log(gap) + workload + log2(memory), and
  an XGBoost classifier. GroupKFold by function. Metrics: ROC-AUC, Brier score,
  calibration curve.
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
On 450 idle-gap invokes across 8 functions (overall cold rate 84%):

| Model (GroupKFold by function) | ROC-AUC | Brier |
|---|---|---|
| **log(gap) logistic** (used by the recommender) | **0.882** | **0.101** |
| XGBoost (gap, workload, memory) | 0.883 | 0.101 |
| Full logistic (gap + log(gap) + workload + memory) | 0.851 | 0.128 |

The log(gap) model matches XGBoost while having a single, well-behaved input.
Workload and memory add noise with only 8 functions.

| Idle gap | 0.1 min | 0.5 min | 1 min | 5 min | 10 min | 20 min | 40 min |
|---|---|---|---|---|---|---|---|
| Model P(cold) | 0.02 | 0.15 | 0.33 | 0.84 | 0.94 | 0.98 | 0.99 |
| Measured | – | – | 0.52 (49/95) | 0.69 (55/80) | 1.00 | 1.00 | 1.00 |

P(cold) = 0.5 at a gap of about **1.6 minutes** (`fig_model_b_curve.png`). The
measured data shows Lambda recycling idle environments quickly: half of the
invokes after a 1-minute gap were already cold, and every invoke after 10 minutes
or more was cold.

### 3.4 Recommendations (real-data inputs)
Number of the 44 functions assigned to each strategy by the model:

| p95 target | req/s | cold rate | on-demand | SnapStart | provisioned |
|---|---|---|---|---|---|
| 500 ms | 0.001 | 0.85 | 14 | 0 | 30 |
| 500 ms | 0.01 | 0.40 | 14 | 0 | 30 |
| 500 ms | 0.1 | 0.04 | 43 | 0 | 1 |
| 500 ms | 1.0 | 0.002 | 43 | 0 | 1 |
| 1000 ms | 0.001 | 0.85 | 23 | 12 | 9 |
| 1000 ms | 0.01 | 0.40 | 23 | 13 | 8 |
| 1000 ms | 0.1 | 0.04 | 43 | 0 | 1 |
| 1000 ms | 1.0 | 0.002 | 43 | 0 | 1 |

**Traffic decides the strategy.**
- **0.1 req/s or more:** a function is called every few seconds, so only about 4% of
  calls start cold. That's below the 5% that a p95 can absorb, so on-demand meets
  either target for 43 of 44 functions at no extra cost.
- **Low traffic (one call every 100–1,000 s):** 40–85% of calls are cold, so heavy
  functions need help.
  - At a 1000 ms target, SnapStart is the cheapest fix for 12–13 functions: almost all
    are the pandas `data` functions (ZIP and layer), whose ~0.56 s restore fits under
    1 s, plus one scikit-learn function. For most `ml` functions the estimated restore
    is above 1 s, so they need provisioned concurrency.
  - At 500 ms, a SnapStart restore (~0.56 s) is already too slow, so 30 functions need
    provisioned concurrency. These include the `web` x86 variants, whose ~500 ms INIT
    sits right at the target.
- **`tiny` stays on-demand in every scenario.** Its ~70 ms INIT never threatens either target.

At the default scenario (1000 ms, 0.1 req/s) the picks total about **$23/month**
for all 44 functions. Before the Model B revision they totalled $208/month, with
provisioned concurrency for 21 functions.

**The one provisioned pick at high traffic is a data artefact.** It is
`data-1024-x86-image`. One of its forced cold starts ran INIT inside the invoke
phase (no `Init Duration` field, 2.6 s duration), so it was recorded as warm and
inflates that function's "warm" p95 (§4).

Full per-function results and the option grid: `ml/notebooks/recommendations.csv`,
`ml/notebooks/recommendations_grid.csv`, `ml/notebooks/recommendation_options.csv`.
These are scenario outputs, not measured strategy outcomes.

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
  not modelled; `recommender.py --cold-floor` can add a minimum cold rate.
- **Model B below 1 minute is an assumption.** No gap shorter than 1 minute was
  measured. log(gap) assumes P(cold) falls smoothly to 0, which is plausible but
  untested. The fitted curve is also a compromise between the measured points: it
  underestimates the 1-minute cold rate (0.33 vs 0.52 measured) and overestimates
  5 minutes (0.84 vs 0.69).
- **One mislabelled cold start:** a forced cold start of `data-1024-x86-image`
  ran INIT inside the invoke phase, so its REPORT line had no `Init Duration` and
  the collector recorded it as warm. It doesn't affect Model A (cold rows only),
  but it inflates that function's warm p95 in the recommender.
- `data`/`ml` have no 128 MB variant, because pandas needs ~130 MB just to import.
- One region (eu-north-1), one runtime (Python 3.12), synthetic handlers.
- Model A is trained on 44 configurations, so it interpolates within this design
  and doesn't generalise to arbitrary code.

## 5. Reproduce
See README → "How to run it" → "Run the ML analysis". Every figure referenced here is in `ml/notebooks/`.
