"""Recommender: cheapest strategy per function that meets a p95 latency target.

For every function and every (latency target, traffic) scenario it compares:

  on_demand    cold rate from Model B; p95 of the mix of cold (INIT + handler)
               and warm (handler) latencies; INIT from the measurements
  snapstart    cold invokes pay a restore instead of INIT. Restore times are
               measured for the functions in the SnapStart run; for the others
               they are estimated as max(RESTORE_FLOOR, RESTORE_RATIO * INIT),
               both fitted from the measured pairs. Not offered for container
               images (Lambda does not support SnapStart on images).
  provisioned  MODELLED, never measured (account concurrency quota): no cold
               starts, p95 = warm handler p95, enough environments for the load

Cost of each comes from ml/cost.py. The pick is the cheapest option whose p95
meets the target; if none does, the one with the lowest p95.

    python ml/recommender.py                       # writes ml/notebooks/recommendations*.csv
    python ml/recommender.py --write               # + DynamoDB table `recommendations`
    python ml/recommender.py --target 500 --rps 0.01 --write

Then: python scripts/publish_recommendations.py   (Grafana panel)
"""
import argparse
import json
import math
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.optimize import brentq
from scipy.stats import norm

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cost import monthly_cost_usd  # noqa: E402
from features import OUT, encode, ensure_out, function_summary, is_mock, lognormal_from_quantiles, load  # noqa: E402
from model_cold import cold_rate  # noqa: E402

SECONDS_PER_MONTH = 30 * 24 * 3600
TARGETS_MS = [500, 1000]
TRAFFIC_RPS = [0.001, 0.01, 0.1, 1.0]
DEFAULT_TARGET_MS = 1000
DEFAULT_RPS = 0.1
RESTORE_FLOOR_MS = 560.0   # defaults if no SnapStart data: measured web/data restores
RESTORE_RATIO = 0.27       # data: ~560 / ~2080
PC_HEADROOM = 1.5
STRATEGIES = ("on_demand", "snapstart", "provisioned")


# ---------- latency model ----------

def mixture_p95(cold_rate_, warm, cold, q=0.95):
    """q-quantile of (1-c) * warm + c * cold, each a lognormal (mu, sigma)."""
    def cdf(x, d):
        return norm.cdf((math.log(x) - d[0]) / d[1]) if d[1] > 0 else float(x >= math.exp(d[0]))

    def f(x):
        return (1 - cold_rate_) * cdf(x, warm) + cold_rate_ * cdf(x, cold) - q

    if cold_rate_ <= 0:
        return math.exp(warm[0] + warm[1] * norm.ppf(q))
    lo, hi = 1e-3, math.exp(max(warm[0] + 6 * warm[1], cold[0] + 6 * cold[1]))
    return brentq(f, lo, hi)


def shifted(p50, p90, add_ms):
    """Lognormal approx of INIT/restore + handler duration."""
    return lognormal_from_quantiles(p50 + add_ms, p90 + add_ms)


# ---------- decision ----------

def choose(options, target_ms):
    """options: list of dicts with strategy, p95_ms, monthly_cost_usd. Returns (best, met_target)."""
    ok = [o for o in options if o["p95_ms"] <= target_ms]
    if ok:
        return min(ok, key=lambda o: (o["monthly_cost_usd"], o["p95_ms"])), True
    return min(options, key=lambda o: (o["p95_ms"], o["monthly_cost_usd"])), False


def restore_estimator(summary):
    """Fit (floor, ratio) from measured (init, restore) pairs, else defaults."""
    m = summary.dropna(subset=["restore_p50_ms"])
    if m.empty:
        return RESTORE_FLOOR_MS, RESTORE_RATIO
    floor = float(m["restore_p50_ms"].min())
    heavy = m[m["init_p50_ms"] > 1.5 * floor]
    ratio = float((heavy["restore_p50_ms"] / heavy["init_p50_ms"]).median()) if not heavy.empty else RESTORE_RATIO
    return floor, ratio


def evaluate(fn, rps, floor_ratio, cold_floor=0.0):
    """All strategy options for one function (row of function_summary) at one traffic level."""
    w, mem, arch = fn["workload"], int(fn["memory_mb"]), fn["arch"]
    dur50, dur95 = fn["duration_p50_ms"], max(fn["duration_p95_ms"], fn["duration_p50_ms"] * 1.01)
    dur_mean = fn["duration_mean_ms"] if pd.notna(fn["duration_mean_ms"]) else dur50
    warm = lognormal_from_quantiles(dur50, dur50 + (dur95 - dur50) * 1.2815 / 1.6449)
    reqs = rps * SECONDS_PER_MONTH
    c = cold_rate(rps, w, mem, floor=cold_floor)
    opts = []

    cold = shifted(fn["init_p50_ms"], fn["init_p90_ms"], dur50)
    cost = monthly_cost_usd("on_demand", mem, arch, reqs, dur_mean, c, init_ms=fn["init_p50_ms"])
    opts.append({"strategy": "on_demand", "cold_rate": c, "p95_ms": mixture_p95(c, warm, cold),
                 "monthly_cost_usd": cost["total"], "restore_source": None})

    if fn["packaging"] != "image":
        if pd.notna(fn.get("restore_p50_ms")):
            r50, r90, src = fn["restore_p50_ms"], fn["restore_p90_ms"], "measured"
        else:
            floor, ratio = floor_ratio
            r50 = max(floor, ratio * fn["init_p50_ms"])
            r90, src = r50 * 1.1, "estimated"
        cost = monthly_cost_usd("snapstart", mem, arch, reqs, dur_mean, c)
        opts.append({"strategy": "snapstart", "cold_rate": c, "p95_ms": mixture_p95(c, warm, shifted(r50, r90, dur50)),
                     "monthly_cost_usd": cost["total"], "restore_source": src})

    pc = max(1, math.ceil(rps * dur_mean / 1000 * PC_HEADROOM))
    cost = monthly_cost_usd("provisioned", mem, arch, reqs, dur_mean, 0.0, pc_concurrency=pc)
    opts.append({"strategy": "provisioned", "cold_rate": 0.0, "p95_ms": mixture_p95(0.0, warm, warm),
                 "monthly_cost_usd": cost["total"], "pc_concurrency": pc, "restore_source": "modelled"})
    return opts


def model_a_predictions(summary):
    """Model A p50/p90 per function (trained models from model_init.py), or NaN if absent."""
    import xgboost as xgb

    cols_path = OUT / "model_a_columns.json"
    if not cols_path.exists():
        print("Model A not trained (run ml/model_init.py); pred_init_* will be measured values")
        return summary["init_p50_ms"], summary["init_p90_ms"]
    X = encode(summary.reset_index(), json.loads(cols_path.read_text()))
    preds = []
    for name in ("p50", "p90"):
        m = xgb.XGBRegressor()
        m.load_model(OUT / f"model_a_{name}.json")
        preds.append(pd.Series(np.exp(m.predict(X)), index=summary.index))
    preds[1] = np.maximum(preds[1], preds[0])  # no quantile crossing
    return preds


def run(inv, targets, traffic, cold_floor=0.0):
    summary = function_summary(inv)
    pred50, pred90 = model_a_predictions(summary)
    fr = restore_estimator(summary)
    all_rows, picks = [], []
    for fid, fn in summary.iterrows():
        for rps in traffic:
            opts = evaluate(fn, rps, fr, cold_floor)
            for target in targets:
                best, met = choose(opts, target)
                for o in opts:
                    all_rows.append({"function_id": fid, "latency_target_ms": target, "traffic_rps": rps, **o,
                                     "chosen": o is best})
                picks.append({
                    "function_id": fid, "workload": fn["workload"], "memory_mb": int(fn["memory_mb"]),
                    "arch": fn["arch"], "packaging": fn["packaging"],
                    "strategy": best["strategy"], "meets_target": met,
                    "pred_init_p50_ms": float(pred50[fid]), "pred_init_p90_ms": float(pred90[fid]),
                    "measured_init_p50_ms": float(fn["init_p50_ms"]),
                    "monthly_cost_usd": best["monthly_cost_usd"], "p95_ms": best["p95_ms"],
                    "cold_rate": best["cold_rate"] if best["strategy"] != "provisioned" else opts[0]["cold_rate"],
                    "latency_target_ms": target, "traffic_rps": rps,
                })
    return pd.DataFrame(picks), pd.DataFrame(all_rows), fr


def write_dynamodb(picks, generated_at, mock):
    import boto3

    table = boto3.resource("dynamodb").Table("recommendations")
    fields = ["strategy", "pred_init_p50_ms", "pred_init_p90_ms", "monthly_cost_usd", "p95_ms",
              "latency_target_ms", "traffic_rps", "meets_target", "cold_rate"]
    with table.batch_writer() as batch:
        for r in picks.to_dict("records"):
            item = {"function_id": r["function_id"], "generated_at": generated_at}
            for k in fields:
                v = r[k]
                item[k] = v if isinstance(v, (str, bool)) else Decimal(str(round(float(v), 6)))
            if mock:
                item["mock"] = True
            batch.put_item(Item=item)
    print(f"wrote {len(picks)} rows to DynamoDB `recommendations`")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", type=float, default=DEFAULT_TARGET_MS, help="p95 target (ms) written to the table")
    ap.add_argument("--rps", type=float, default=DEFAULT_RPS, help="traffic (req/s) written to the table")
    ap.add_argument("--cold-floor", type=float, default=0.0,
                    help="minimum cold rate (scale-out / recycling not in Model B)")
    ap.add_argument("--write", action="store_true", help="write the chosen scenario to DynamoDB")
    args = ap.parse_args()

    inv, _ = load()
    mock = is_mock(inv)
    out = ensure_out()
    targets = sorted(set(TARGETS_MS + [args.target]))
    traffic = sorted(set(TRAFFIC_RPS + [args.rps]))
    picks, options, (floor, ratio) = run(inv, targets, traffic, args.cold_floor)
    picks.to_csv(out / "recommendations_grid.csv", index=False)
    options.to_csv(out / "recommendation_options.csv", index=False)

    main_s = picks[(picks["latency_target_ms"] == args.target) & (picks["traffic_rps"] == args.rps)]
    main_s.to_csv(out / "recommendations.csv", index=False)
    label = " [MOCK DATA]" if mock else ""
    print(f"SnapStart restore estimate: max({floor:.0f} ms, {ratio:.2f} x INIT)")
    print(f"\nRecommendations at p95 <= {args.target:.0f} ms, {args.rps} req/s{label}:")
    print(main_s[["function_id", "strategy", "meets_target", "p95_ms", "monthly_cost_usd", "cold_rate"]]
          .round({"p95_ms": 0, "monthly_cost_usd": 3, "cold_rate": 3}).to_string(index=False))
    print("\nStrategy counts per scenario:")
    print(picks.groupby(["latency_target_ms", "traffic_rps"])["strategy"].value_counts().unstack(fill_value=0))

    if args.write:
        write_dynamodb(main_s, datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z", mock)


if __name__ == "__main__":
    pd.set_option("display.width", 140)
    main()
