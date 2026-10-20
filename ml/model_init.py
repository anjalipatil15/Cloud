"""Model A: predict cold-start INIT time (p50 and p90) from function configuration.

XGBoost quantile regression (reg:quantileerror, alpha 0.5 and 0.9), validated with
GroupKFold by function_id: the 30 samples of one function are near-identical,
so letting them span folds would leak. Baseline: median INIT per (workload, arch).

    python ml/model_init.py

Writes ml/notebooks/:
  model_a_metrics.json      MAE / MAPE (p50), coverage + pinball loss (p90), baseline
  model_a_cv_predictions.csv  out-of-fold predictions per function
  model_a_p50.json, model_a_p90.json  models trained on all data (used by recommender)
  fig_model_a_pred.png      predicted vs measured
  fig_shap_summary.png, fig_shap_bar.png  SHAP for the p50 model
"""
import json

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
import xgboost as xgb  # noqa: E402
from sklearn.model_selection import GroupKFold  # noqa: E402

from features import encode, ensure_out, is_mock, load, model_a_table, tag  # noqa: E402

PARAMS = dict(n_estimators=300, max_depth=3, learning_rate=0.05, subsample=0.9, min_child_weight=5)
N_SPLITS = 5


def quantile_model(alpha):
    return xgb.XGBRegressor(objective="reg:quantileerror", quantile_alpha=alpha, tree_method="hist", **PARAMS)


def pinball(y, q, alpha):
    diff = y - q
    return float(np.mean(np.maximum(alpha * diff, (alpha - 1) * diff)))


def per_function(d, col):
    return d.groupby("function_id").agg(true=("init_ms", "median"), pred=(col, "median"))


def cross_validate(d):
    """Out-of-fold predictions for p50, p90 and the baseline."""
    X, y, groups = encode(d), np.log(d["init_ms"].to_numpy()), d["function_id"].to_numpy()
    oof = d[["function_id", "workload", "arch", "packaging", "memory_mb", "init_ms"]].copy()
    for name in ("pred_p50", "pred_p90", "baseline"):
        oof[name] = np.nan
    for train, test in GroupKFold(n_splits=N_SPLITS).split(X, y, groups):
        for alpha, name in ((0.5, "pred_p50"), (0.9, "pred_p90")):
            m = quantile_model(alpha).fit(X.iloc[train], y[train])
            oof.iloc[test, oof.columns.get_loc(name)] = np.exp(m.predict(X.iloc[test]))
        tr = d.iloc[train]
        base = tr.groupby(["workload", "arch"])["init_ms"].median()
        fallback = tr.groupby("workload")["init_ms"].median()
        oof.iloc[test, oof.columns.get_loc("baseline")] = [
            base.get((w, a), fallback.get(w, tr["init_ms"].median())) for w, a in zip(d["workload"].iloc[test], d["arch"].iloc[test])
        ]
    # Independent quantile models can cross; enforce p90 >= p50.
    oof["pred_p90"] = np.maximum(oof["pred_p90"], oof["pred_p50"])
    return oof


def metrics(oof):
    fn = oof.groupby("function_id").agg(true=("init_ms", "median"), p50=("pred_p50", "median"), base=("baseline", "median"))
    ape = lambda p: float(np.mean(np.abs(fn[p] - fn["true"]) / fn["true"]) * 100)  # noqa: E731
    return {
        "n_rows": int(len(oof)),
        "n_functions": int(oof["function_id"].nunique()),
        "cv": f"GroupKFold(n_splits={N_SPLITS}) by function_id",
        "p50": {
            "mae_ms_per_function": float(np.mean(np.abs(fn["p50"] - fn["true"]))),
            "mape_pct_per_function": ape("p50"),
            "mae_ms_per_row": float(np.mean(np.abs(oof["pred_p50"] - oof["init_ms"]))),
            "pinball": pinball(oof["init_ms"], oof["pred_p50"], 0.5),
        },
        "p90": {
            "coverage": float(np.mean(oof["init_ms"] <= oof["pred_p90"])),
            "pinball": pinball(oof["init_ms"], oof["pred_p90"], 0.9),
        },
        "baseline_median_by_workload_arch": {
            "mae_ms_per_function": float(np.mean(np.abs(fn["base"] - fn["true"]))),
            "mape_pct_per_function": ape("base"),
        },
    }


def fig_pred(oof, out, t):
    fn = oof.groupby("function_id").agg(true=("init_ms", "median"), p50=("pred_p50", "median"),
                                        p90=("pred_p90", "median"), workload=("workload", "first"))
    fig, ax = plt.subplots(figsize=(5.5, 5))
    colors = {"tiny": "#4C72B0", "web": "#55A868", "data": "#C44E52", "ml": "#8172B2"}
    for w, g in fn.groupby("workload"):
        ax.errorbar(g["true"], g["p50"], yerr=[np.zeros(len(g)), g["p90"] - g["p50"]], fmt="o", color=colors.get(w),
                    label=w, alpha=0.8, capsize=2)
    lim = [fn[["true", "p50"]].min().min() * 0.7, fn[["true", "p90"]].max().max() * 1.3]
    ax.plot(lim, lim, "k--", lw=1)
    ax.set_xscale("log")
    ax.set_yscale("log")
    ax.set_xlim(lim)
    ax.set_ylim(lim)
    ax.set_xlabel("measured median INIT (ms)")
    ax.set_ylabel("predicted p50 (bar to p90), out-of-fold")
    ax.set_title(f"Model A, GroupKFold by function{t}", fontsize=10)
    ax.legend()
    plt.tight_layout()
    plt.savefig(out / "fig_model_a_pred.png", dpi=150)
    plt.close()


def shap_plots(model, X, out, t):
    import shap

    explainer = shap.TreeExplainer(model)
    sv = explainer(X)
    shap.summary_plot(sv, X, show=False)
    plt.title(f"SHAP: Model A p50 (log INIT){t}", fontsize=10)
    plt.tight_layout()
    plt.savefig(out / "fig_shap_summary.png", dpi=150)
    plt.close()
    shap.summary_plot(sv, X, plot_type="bar", show=False)
    plt.title(f"Mean |SHAP|: Model A p50{t}", fontsize=10)
    plt.tight_layout()
    plt.savefig(out / "fig_shap_bar.png", dpi=150)
    plt.close()
    imp = pd.Series(np.abs(sv.values).mean(0), index=X.columns).sort_values(ascending=False)
    return imp


def main():
    inv, _ = load()
    t = tag(inv)
    out = ensure_out()
    d = model_a_table(inv)
    print(f"Model A table: {len(d)} cold starts, {d['function_id'].nunique()} functions{t}")

    oof = cross_validate(d)
    oof.to_csv(out / "model_a_cv_predictions.csv", index=False)
    m = metrics(oof)

    X, y = encode(d), np.log(d["init_ms"].to_numpy())
    final = {}
    for alpha, name in ((0.5, "p50"), (0.9, "p90")):
        final[name] = quantile_model(alpha).fit(X, y)
        final[name].save_model(out / f"model_a_{name}.json")
    (out / "model_a_columns.json").write_text(json.dumps(list(X.columns)))

    fig_pred(oof, out, t)
    imp = shap_plots(final["p50"], X, out, t)
    m["shap_mean_abs_p50"] = {k: round(float(v), 4) for k, v in imp.items()}
    m["mock"] = is_mock(inv)
    (out / "model_a_metrics.json").write_text(json.dumps(m, indent=2))

    print(json.dumps({k: m[k] for k in ("p50", "p90", "baseline_median_by_workload_arch")}, indent=2))
    print(f"SHAP mean |value| (top 6):\n{imp.head(6).round(3)}")


if __name__ == "__main__":
    main()
