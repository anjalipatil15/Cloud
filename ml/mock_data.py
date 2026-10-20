"""Mock dataset matching docs/data_contract.md, so the ML code runs before (or
without) the real export. Numbers follow the medians in docs/HANDOVER_ML.md.

    python ml/mock_data.py                # writes data/invocations.csv, data/import_profiles.csv
    python ml/mock_data.py --out mock

Every row gets run_id prefixed as in the real runs, and a `mock` column = True so
nothing produced from it can be mistaken for a measurement. Do NOT run this over
a real export: it refuses to overwrite files that have no `mock` column.
"""
import argparse
import csv
import itertools
import math
import random
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cost import init_cost_usd  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]

# Median INIT ms on x86 / arm64 for ZIP at 512 MB (handover table).
INIT_MS = {"tiny": (78, 68), "web": (500, 445), "data": (2050, 1785), "ml": (3800, 3450)}
IMAGE_INIT_MS = {"x86": 700, "arm64": 640}          # data, container image
WARM_MS = {"tiny": 1.5, "web": 12, "data": 35, "ml": 55}  # handler duration at 1024 MB
PACKAGE_MB = {"tiny": 0.01, "web": 7.5, "data": 68, "ml": 95}
N_IMPORTS = {"tiny": 18, "web": 260, "data": 940, "ml": 1350}
SNAPSTART_RESTORE_MS = {"web-512-x86-zip": 585, "data-512-x86-zip": 560}
IDLE_GAP_FUNCS = [f"{w}-{m}-arm64-zip" for w in ("tiny", "web", "data", "ml") for m in (512, 1024)]
TOP_MODULES = {
    "tiny": [("json", 2100), ("encodings", 1400)],
    "web": [("pydantic", 210000), ("requests", 120000), ("jinja2", 90000), ("urllib3", 60000), ("pydantic_core", 55000)],
    "data": [("pandas", 1450000), ("numpy", 260000), ("pandas.core.frame", 310000), ("pytz", 40000), ("dateutil", 35000)],
    "ml": [("sklearn", 1900000), ("scipy", 950000), ("numpy", 260000), ("joblib", 120000), ("threadpoolctl", 15000)],
}

COLUMNS = [
    "function_id", "request_id", "ts", "run_id", "strategy", "forced",
    "workload", "memory_mb", "arch", "packaging", "package_mb", "n_imports",
    "idle_gap_min", "is_cold", "init_ms", "restore_ms", "billed_restore_ms",
    "duration_ms", "billed_ms", "max_mem_mb", "init_cost_usd", "client_ms", "version", "log_stream", "mock",
]
PROFILE_COLUMNS = ["function_id", "module", "self_us", "cumulative_us", "log_stream", "ts", "mock"]


def functions():
    core = [
        c for c in itertools.product(["tiny", "web", "data", "ml"], [128, 256, 512, 1024, 2048], ["x86", "arm64"], ["zip"])
        if not (c[0] in ("data", "ml") and c[1] == 128)
    ]
    packaging = list(itertools.product(["data"], [512, 1024], ["x86", "arm64"], ["layer", "image"]))
    return core + packaging


def fid(w, m, a, p):
    return f"{w}-{m}-{a}-{p}"


def median_init(w, m, a, p):
    base = IMAGE_INIT_MS[a] if p == "image" else INIT_MS[w][0 if a == "x86" else 1]
    # Memory barely matters: ~+5% from 256 MB to 2 GB, a bit slower at 128 MB.
    mem = 1 + 0.025 * math.log2(m / 512) + (0.08 if m == 128 else 0)
    return base * mem


def warm_ms(w, m):
    return WARM_MS[w] * (1024 / m) ** 0.6  # CPU scales with memory


class Clock:
    def __init__(self, start):
        self.t = start

    def tick(self, seconds):
        self.t += timedelta(seconds=seconds)
        return self.t.strftime("%Y-%m-%dT%H:%M:%S.") + f"{self.t.microsecond // 1000:03d}Z"


def row(rng, clock, f, run_id, strategy, cold, init=None, restore=None, gap=None, forced=False, version=None):
    w, m, a, p = f
    d = warm_ms(w, m) * rng.lognormvariate(0, 0.15)
    r = {
        "function_id": fid(*f), "request_id": str(uuid.UUID(int=rng.getrandbits(128))), "ts": clock.tick(rng.uniform(1, 30)),
        "run_id": run_id, "strategy": strategy, "forced": forced, "workload": w, "memory_mb": m, "arch": a,
        "packaging": p, "package_mb": PACKAGE_MB[w] * (4.5 if p == "image" else 1), "is_cold": cold,
        "duration_ms": round(d, 2), "billed_ms": math.ceil(d), "max_mem_mb": {"tiny": 40, "web": 75, "data": 150, "ml": 190}[w],
        "mock": True,
    }
    if gap is not None:
        r["idle_gap_min"] = round(gap, 2)
    if version:
        r["version"] = version
    if cold:
        r["log_stream"] = f"2026/10/19/[$LATEST]{rng.getrandbits(64):016x}"
        r["n_imports"] = N_IMPORTS[w] + rng.randint(-3, 3)
    if init is not None:
        r["init_ms"] = round(init, 2)
        r["init_cost_usd"] = init_cost_usd(m, a, init)
    if restore is not None:
        r["restore_ms"] = round(restore, 2)
        r["billed_restore_ms"] = math.ceil(restore)
    if strategy == "snapstart":
        r["client_ms"] = round((restore or 0) + d + rng.uniform(25, 60), 2)
    return r


def p_cold(gap_min, w):
    """True cold-start probability vs idle gap for the mock (env reclaimed ~5-15 min)."""
    mid = 9 if w in ("tiny", "web") else 8
    return 1 / (1 + math.exp(-(gap_min - mid) / 1.8))


def generate(seed=7):
    rng = random.Random(seed)
    clock = Clock(datetime(2026, 10, 19, 6, 0, tzinfo=timezone.utc))
    rows = []

    # Core sweep + packaging: 30 forced cold starts each; one comes back warm (as in the real run).
    for f in functions():
        med = median_init(*f)
        for _ in range(30):
            rows.append(row(rng, clock, f, "core-sweep-20261019", "on_demand", True,
                            init=med * rng.lognormvariate(0, 0.06), forced=True))
    warm_one = next(r for r in rows if r["function_id"] == "web-1024-x86-zip")
    for k in ("init_ms", "init_cost_usd", "log_stream", "n_imports"):
        warm_one.pop(k, None)
    warm_one["is_cold"] = False

    # SnapStart: 30 restores + ~25 warm calls on two functions.
    for name, restore in SNAPSTART_RESTORE_MS.items():
        f = tuple(int(x) if x.isdigit() else x for x in name.split("-"))
        for i in range(30):
            rows.append(row(rng, clock, f, "snapstart-20261019", "snapstart", True,
                            restore=restore * rng.lognormvariate(0, 0.05), forced=True, version=str(2 + i // 10)))
        for _ in range(25):
            rows.append(row(rng, clock, f, "snapstart-20261019", "snapstart", False, version="4"))

    # Idle-gap chains: ~24 h, gaps uniform 1-40 min, 8 functions.
    for name in IDLE_GAP_FUNCS:
        f = tuple(int(x) if x.isdigit() else x for x in name.split("-"))
        med = median_init(*f)
        rows.append(row(rng, clock, f, "idle-gap-20261019", "on_demand", True, init=med))  # first invoke: no gap
        elapsed = 0.0
        while elapsed < 24 * 60:
            gap = rng.uniform(1, 40)
            elapsed += gap
            cold = rng.random() < p_cold(gap, f[0])
            rows.append(row(rng, clock, f, "idle-gap-20261019", "on_demand", cold,
                            init=med * rng.lognormvariate(0, 0.06) if cold else None, gap=gap))

    profiles = []
    for f in functions():
        w = f[0]
        stream = next(r["log_stream"] for r in reversed(rows) if r["function_id"] == fid(*f) and r.get("log_stream"))
        scale = median_init(*f) / median_init(w, 512, "x86", "zip") if w != "tiny" else 1
        for mod, cum in TOP_MODULES[w]:
            c = int(cum * scale * rng.lognormvariate(0, 0.05))
            profiles.append({"function_id": fid(*f), "module": mod, "self_us": int(c * rng.uniform(0.02, 0.15)),
                             "cumulative_us": c, "log_stream": stream, "ts": "2026-10-19T12:00:00.000Z", "mock": True})
    return rows, profiles


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "data"))
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    target = out / "invocations.csv"
    if target.exists() and "mock" not in target.open().readline():
        sys.exit(f"{target} looks like a real export; refusing to overwrite it")

    rows, profiles = generate()
    for path, cols, data in ((target, COLUMNS, rows), (out / "import_profiles.csv", PROFILE_COLUMNS, profiles)):
        with path.open("w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=cols)
            w.writeheader()
            w.writerows(data)
        print(f"{path}: {len(data)} rows (MOCK)")


if __name__ == "__main__":
    main()
