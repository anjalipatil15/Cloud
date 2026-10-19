"""Cold-start cost meter. All prices come from pricing.json next to this file.

Used by the collector (init_cost_usd per invocation) and by the recommender
(monthly cost per strategy). Standard library only, so the collector can ship it.
"""
import json
import math
from pathlib import Path

PRICING = json.loads((Path(__file__).with_name("pricing.json")).read_text())
SECONDS_PER_MONTH = 30 * 24 * 3600


def _gb(memory_mb):
    return memory_mb / 1024


def init_cost_usd(memory_mb, arch, init_ms):
    """Cost of one INIT phase (billed like normal duration since Aug 2025)."""
    return _gb(memory_mb) * (init_ms / 1000) * PRICING["duration_gb_s"][arch]


def restore_cost_usd(memory_mb):
    """Cost of one SnapStart restore."""
    return _gb(memory_mb) * PRICING["snapstart"]["restore_gb"]


def monthly_cost_usd(
    strategy,
    memory_mb,
    arch,
    requests_per_month,
    avg_duration_ms,
    cold_rate,
    init_ms=0.0,
    pc_concurrency=0,
):
    """Projected monthly cost for one function under a strategy.

    strategy: "on_demand" | "snapstart" | "provisioned"
    cold_rate: fraction of requests that start cold under on-demand/snapstart
    init_ms: average INIT duration (on-demand cold starts)
    pc_concurrency: provisioned environments kept warm all month
    Returns a dict with the parts and the total.
    """
    gb = _gb(memory_mb)
    cold_requests = requests_per_month * cold_rate
    parts = {"requests": requests_per_month / 1e6 * PRICING["request_per_million"]}

    if strategy == "provisioned":
        pc = PRICING["provisioned"]
        parts["pc_idle"] = pc_concurrency * gb * SECONDS_PER_MONTH * pc["concurrency_gb_s"][arch]
        parts["duration"] = requests_per_month * gb * avg_duration_ms / 1000 * pc["duration_gb_s"][arch]
    else:
        parts["duration"] = requests_per_month * gb * avg_duration_ms / 1000 * PRICING["duration_gb_s"][arch]
        if strategy == "on_demand":
            parts["init"] = cold_requests * init_cost_usd(memory_mb, arch, init_ms)
        elif strategy == "snapstart":
            ss = PRICING["snapstart"]
            parts["snapshot_cache"] = gb * max(SECONDS_PER_MONTH, ss["min_cache_seconds"]) * ss["cache_gb_s"]
            parts["restore"] = cold_requests * restore_cost_usd(memory_mb)
        else:
            raise ValueError(f"unknown strategy {strategy!r}")

    parts = {k: round(v, 6) for k, v in parts.items()}
    parts["total"] = round(math.fsum(parts.values()), 6)
    return parts


if __name__ == "__main__":
    # Example: data-512-arm64, 100k requests/month, 5% cold, 800 ms init.
    for s in ("on_demand", "snapstart", "provisioned"):
        print(s, monthly_cost_usd(s, 512, "arm64", 100_000, 120, 0.05, init_ms=800, pc_concurrency=1))
