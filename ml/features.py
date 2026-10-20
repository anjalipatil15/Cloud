"""Load the CSV export and build the tables every ML script uses.

    from features import load, model_a_table, model_b_table, function_summary

Works on the real export (scripts/export_csv.py) or the mock (ml/mock_data.py).
"""
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
OUT = ROOT / "ml" / "notebooks"

CORE_RUN = "core-sweep"
IDLE_RUN = "idle-gap"
SNAP_RUN = "snapstart"
FEATURES_A = ["workload", "memory_mb", "arch", "packaging", "package_mb", "n_imports"]
CATEGORICAL = ["workload", "arch", "packaging"]
WORKLOAD_ORDER = ["tiny", "web", "data", "ml"]


def _bool(s):
    return s.astype(str).str.lower().isin(["true", "1"])


def load(data_dir=DATA):
    inv = pd.read_csv(Path(data_dir) / "invocations.csv")
    for col in ("is_cold", "forced"):
        if col in inv:
            inv[col] = _bool(inv[col])
    inv["is_mock"] = _bool(inv["mock"]) if "mock" in inv else False
    num = ["memory_mb", "package_mb", "n_imports", "idle_gap_min", "init_ms", "restore_ms",
           "duration_ms", "billed_ms", "client_ms", "init_cost_usd"]
    for col in num:
        if col in inv:
            inv[col] = pd.to_numeric(inv[col], errors="coerce")
    inv["run_id"] = inv["run_id"].fillna("")
    prof_path = Path(data_dir) / "import_profiles.csv"
    prof = pd.read_csv(prof_path) if prof_path.exists() else pd.DataFrame()
    return inv, prof


def is_mock(inv):
    return bool(inv["is_mock"].any())


def tag(inv):
    """Label to stamp on figures/outputs."""
    return " [MOCK DATA]" if is_mock(inv) else ""


def model_a_table(inv):
    """Cold starts from the core sweep (incl. packaging variants), one row per INIT."""
    d = inv[inv["run_id"].str.startswith(CORE_RUN) & inv["is_cold"] & inv["init_ms"].notna()].copy()
    # n_imports is per function in practice; fill gaps (rows whose counter was not joined) per function.
    d["n_imports"] = d.groupby("function_id")["n_imports"].transform(lambda s: s.fillna(s.median()))
    d["package_mb"] = d.groupby("function_id")["package_mb"].transform(lambda s: s.fillna(s.median()))
    return d.reset_index(drop=True)


def encode(d, columns=None):
    """One-hot categorical features for XGBoost / sklearn. Pass training columns to align."""
    X = pd.get_dummies(d[FEATURES_A], columns=CATEGORICAL, dtype=float)
    if columns is not None:
        X = X.reindex(columns=columns, fill_value=0.0)
    return X


def model_b_table(inv):
    """Idle-gap invokes with a gap (first invoke of each chain dropped)."""
    d = inv[inv["run_id"].str.startswith(IDLE_RUN) & inv["idle_gap_min"].notna()].copy()
    return d.reset_index(drop=True)


def function_summary(inv):
    """Per-function measured stats used by figures and the recommender."""
    core = model_a_table(inv)
    warm = inv[~inv["is_cold"] & inv["duration_ms"].notna()]
    allrows = inv[inv["duration_ms"].notna()]
    g = core.groupby("function_id")
    s = pd.DataFrame({
        "workload": g["workload"].first(),
        "memory_mb": g["memory_mb"].first(),
        "arch": g["arch"].first(),
        "packaging": g["packaging"].first(),
        "package_mb": g["package_mb"].median(),
        "n_imports": g["n_imports"].median(),
        "n_cold": g.size(),
        "init_p50_ms": g["init_ms"].median(),
        "init_p90_ms": g["init_ms"].quantile(0.9),
    })
    # Handler duration: warm rows where present, else all rows (core sweep is all cold).
    dur = warm.groupby("function_id")["duration_ms"]
    dur_all = allrows.groupby("function_id")["duration_ms"]
    s["duration_p50_ms"] = dur.median().reindex(s.index).fillna(dur_all.median())
    s["duration_p95_ms"] = dur.quantile(0.95).reindex(s.index).fillna(dur_all.quantile(0.95))
    s["duration_mean_ms"] = dur_all.mean().reindex(s.index)

    snap = inv[inv["run_id"].str.startswith(SNAP_RUN) & inv["restore_ms"].notna()]
    sg = snap.groupby("function_id")["restore_ms"]
    s["restore_p50_ms"] = sg.median().reindex(s.index)
    s["restore_p90_ms"] = sg.quantile(0.9).reindex(s.index)
    return s.sort_values(["workload", "packaging", "arch", "memory_mb"])


def ensure_out():
    OUT.mkdir(parents=True, exist_ok=True)
    return OUT


def lognormal_from_quantiles(p50, p90):
    """(mu, sigma) of a lognormal with the given median and p90."""
    p50 = max(float(p50), 1e-6)
    p90 = max(float(p90), p50 * 1.0001)
    return np.log(p50), (np.log(p90) - np.log(p50)) / 1.2815515655446004
