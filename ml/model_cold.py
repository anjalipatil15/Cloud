"""Model B: probability that an invoke starts cold, given the idle gap before it.

Two models on the idle-gap run, both validated with GroupKFold by function_id:
  - logistic regression on idle_gap_min (+ workload, memory): simple, calibrated,
    and exported as coefficients for the recommender
  - XGBoost classifier on the same features, for comparison

    python ml/model_cold.py

Writes ml/notebooks/: model_b_metrics.json, model_b_logistic.json,
fig_model_b_curve.png (P(cold) vs gap), fig_model_b_calibration.png.

The recommender turns P(cold | gap) into a cold-start *rate* for a traffic level
with cold_rate(): with Poisson arrivals at r req/s, gaps are exponential with
mean 1/r, so rate = E[P(cold | gap)].
"""
import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

OUT = Path(__file__).resolve().parents[1] / "ml" / "notebooks"


def _design(d, columns=None):
    X = pd.DataFrame({"idle_gap_min": d["idle_gap_min"].astype(float)})
    X = X.join(pd.get_dummies(d["workload"], prefix="workload", dtype=float))
    X["log2_memory"] = np.log2(d["memory_mb"].astype(float) / 512)
    if columns is not None:
        X = X.reindex(columns=columns, fill_value=0.0)
    return X


def p_cold(gap_min, workload=None, memory_mb=512, coef_path=OUT / "model_b_logistic.json"):
    """P(cold | idle gap) from the exported logistic model. gap_min may be an array."""
    c = json.loads(Path(coef_path).read_text())
    z = c["intercept"] + c["coef"]["idle_gap_min"] * np.asarray(gap_min, dtype=float)
    z = z + c["coef"].get(f"workload_{workload}", 0.0) + c["coef"].get("log2_memory", 0.0) * np.log2(memory_mb / 512)
    return 1 / (1 + np.exp(-z))


def cold_rate(traffic_rps, workload=None, memory_mb=512, coef_path=OUT / "model_b_logistic.json", floor=0.0):
    """Expected fraction of cold invokes at a steady Poisson traffic level.

    Ignores scale-out (concurrent requests needing extra environments) and the
    periodic recycling of environments; `floor` lets the caller add a minimum.
    """
    if traffic_rps <= 0:
        return 1.0
    mean_gap_min = 1 / traffic_rps / 60
    # E[f(G)] for G ~ Exp(mean): integrate on a quantile grid (robust for tiny/huge means).
    u = (np.arange(4000) + 0.5) / 4000
    gaps = -mean_gap_min * np.log(1 - u)
    return float(max(floor, np.mean(p_cold(gaps, workload, memory_mb, coef_path))))


def main():
    from sklearn.calibration import calibration_curve
    from sklearn.linear_model import LogisticRegression
    from sklearn.metrics import brier_score_loss, roc_auc_score
    from sklearn.model_selection import GroupKFold
    import xgboost as xgb

    from features import ensure_out, is_mock, load, model_b_table, tag

    inv, _ = load()
    t = tag(inv)
    out = ensure_out()
    d = model_b_table(inv)
    if d.empty:
        raise SystemExit("no idle-gap rows with idle_gap_min; export after the idle-gap run")
    y = d["is_cold"].astype(int).to_numpy()
    X = _design(d)
    groups = d["function_id"].to_numpy()
    n_splits = min(4, d["function_id"].nunique())
    print(f"Model B table: {len(d)} invokes, {d['function_id'].nunique()} functions, cold rate {y.mean():.2f}{t}")

    models = {
        "logistic": lambda: LogisticRegression(C=10.0, max_iter=2000),
        "xgboost": lambda: xgb.XGBClassifier(n_estimators=200, max_depth=2, learning_rate=0.05, tree_method="hist"),
        "logistic_gap_only": lambda: LogisticRegression(C=10.0, max_iter=2000),
    }
    oof = {k: np.zeros(len(d)) for k in models}
    for train, test in GroupKFold(n_splits=n_splits).split(X, y, groups):
        for name, make in models.items():
            cols = ["idle_gap_min"] if name == "logistic_gap_only" else X.columns
            m = make().fit(X.iloc[train][cols], y[train])
            oof[name][test] = m.predict_proba(X.iloc[test][cols])[:, 1]

    metrics = {"n_rows": int(len(d)), "n_functions": int(d["function_id"].nunique()), "cold_rate": float(y.mean()),
               "cv": f"GroupKFold(n_splits={n_splits}) by function_id", "mock": is_mock(inv)}
    for name, p in oof.items():
        metrics[name] = {"roc_auc": float(roc_auc_score(y, p)), "brier": float(brier_score_loss(y, p))}

    # Final logistic on all data -> coefficients for the recommender.
    final = LogisticRegression(C=10.0, max_iter=2000).fit(X, y)
    coef = {"intercept": float(final.intercept_[0]), "coef": dict(zip(X.columns, map(float, final.coef_[0]))),
            "features": list(X.columns), "mock": is_mock(inv)}
    (out / "model_b_logistic.json").write_text(json.dumps(coef, indent=2))
    # Gap at which P(cold) = 0.5, per workload at 512 MB.
    metrics["gap_min_at_p50"] = {
        w: float(-(coef["intercept"] + coef["coef"].get(f"workload_{w}", 0)) / coef["coef"]["idle_gap_min"])
        for w in sorted(d["workload"].unique())
    }
    (out / "model_b_metrics.json").write_text(json.dumps(metrics, indent=2))

    # Calibration plot (out-of-fold).
    fig, ax = plt.subplots(figsize=(5, 5))
    for name, p in oof.items():
        frac, mean = calibration_curve(y, p, n_bins=8, strategy="quantile")
        ax.plot(mean, frac, "o-", label=f"{name} (AUC {metrics[name]['roc_auc']:.2f})")
    ax.plot([0, 1], [0, 1], "k--", lw=1)
    ax.set_xlabel("predicted P(cold)")
    ax.set_ylabel("observed cold fraction")
    ax.set_title(f"Model B calibration, out-of-fold{t}", fontsize=10)
    ax.legend(fontsize=8)
    plt.tight_layout()
    plt.savefig(out / "fig_model_b_calibration.png", dpi=150)
    plt.close()

    # P(cold) vs gap: binned observations + fitted curve per workload.
    fig, ax = plt.subplots(figsize=(7, 4))
    grid = np.linspace(0, d["idle_gap_min"].max(), 200)
    colors = {"tiny": "#4C72B0", "web": "#55A868", "data": "#C44E52", "ml": "#8172B2"}
    bins = np.arange(0, d["idle_gap_min"].max() + 5, 5)
    for w, g in d.groupby("workload"):
        b = g.groupby(pd.cut(g["idle_gap_min"], bins), observed=True)["is_cold"].mean()
        ax.plot([iv.mid for iv in b.index], b.values, "o", color=colors.get(w), alpha=0.6)
        ax.plot(grid, p_cold(grid, w, 512, out / "model_b_logistic.json"), "-", color=colors.get(w), label=w)
    ax.set_xlabel("idle gap before invoke (min)")
    ax.set_ylabel("P(cold)")
    ax.set_title(f"Cold-start probability vs idle gap (dots: 5-min bins){t}", fontsize=10)
    ax.legend()
    plt.tight_layout()
    plt.savefig(out / "fig_model_b_curve.png", dpi=150)
    plt.close()

    print(json.dumps({k: v for k, v in metrics.items() if isinstance(v, dict)}, indent=2))


if __name__ == "__main__":
    main()
