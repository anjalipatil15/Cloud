"""Report figures: arch, memory, packaging, SnapStart and import-time comparisons.

    python ml/figures.py      # writes ml/notebooks/fig_*.png and summary_by_function.csv
"""
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import pandas as pd  # noqa: E402

from features import WORKLOAD_ORDER, ensure_out, function_summary, load, model_a_table, tag  # noqa: E402

ARCH_COLORS = {"x86": "#4C72B0", "arm64": "#DD8452"}


def fig_arch(d, out, t):
    zip512 = d[(d["packaging"] == "zip")]
    med = zip512.groupby(["workload", "arch"])["init_ms"].median().unstack().reindex(WORKLOAD_ORDER)
    ax = med[["x86", "arm64"]].plot.bar(color=[ARCH_COLORS["x86"], ARCH_COLORS["arm64"]], figsize=(7, 4), rot=0)
    ax.set_yscale("log")
    ax.set_ylabel("median INIT (ms, log)")
    ax.set_title(f"Cold-start INIT by workload and architecture (ZIP, all memory){t}")
    for c in ax.containers:
        ax.bar_label(c, fmt="%.0f", fontsize=8)
    plt.tight_layout()
    plt.savefig(out / "fig_arch.png", dpi=150)
    plt.close()
    return med


def fig_memory(d, out, t):
    zipd = d[d["packaging"] == "zip"]
    fig, axes = plt.subplots(1, 4, figsize=(14, 3.6))
    for ax, w in zip(axes, WORKLOAD_ORDER):
        for arch, color in ARCH_COLORS.items():
            m = zipd[(zipd["workload"] == w) & (zipd["arch"] == arch)].groupby("memory_mb")["init_ms"]
            ax.errorbar(m.median().index, m.median(), yerr=[m.median() - m.quantile(0.1), m.quantile(0.9) - m.median()],
                        marker="o", capsize=3, label=arch, color=color)
        ax.set_xscale("log", base=2)
        ax.set_title(w)
        ax.set_xlabel("memory (MB)")
        ax.set_ylim(bottom=0)
    axes[0].set_ylabel("INIT ms (p10–p50–p90)")
    axes[0].legend()
    fig.suptitle(f"Memory size barely changes INIT time{t}")
    plt.tight_layout()
    plt.savefig(out / "fig_memory.png", dpi=150)
    plt.close()


def fig_packaging(d, out, t):
    p = d[(d["workload"] == "data") & d["memory_mb"].isin([512, 1024])]
    med = p.groupby(["arch", "packaging"])["init_ms"].median().unstack()[["zip", "layer", "image"]]
    ax = med.plot.bar(figsize=(6, 4), rot=0, color=["#55A868", "#8172B2", "#C44E52"])
    ax.set_ylabel("median INIT (ms)")
    ax.set_title(f"`data` workload: packaging (512 + 1024 MB){t}")
    for c in ax.containers:
        ax.bar_label(c, fmt="%.0f", fontsize=8)
    plt.tight_layout()
    plt.savefig(out / "fig_packaging.png", dpi=150)
    plt.close()
    return med


def fig_snapstart(summary, out, t):
    s = summary.dropna(subset=["restore_p50_ms"])
    if s.empty:
        print("no SnapStart rows; skipping fig_snapstart")
        return None
    s = s[["init_p50_ms", "restore_p50_ms"]].rename(columns={"init_p50_ms": "on-demand INIT", "restore_p50_ms": "SnapStart restore"})
    ax = s.plot.bar(figsize=(6, 4), rot=0, color=["#4C72B0", "#55A868"])
    ax.set_ylabel("median ms")
    ax.set_title(f"SnapStart restore vs on-demand INIT{t}")
    for c in ax.containers:
        ax.bar_label(c, fmt="%.0f", fontsize=8)
    plt.tight_layout()
    plt.savefig(out / "fig_snapstart.png", dpi=150)
    plt.close()
    return s


def fig_imports(prof, out, t):
    if prof.empty:
        print("no import profiles; skipping fig_imports")
        return
    prof = prof[~prof["module"].astype(str).str.startswith("#stream")].copy()
    prof["workload"] = prof["function_id"].str.split("-").str[0]
    # Top-level packages only, so nested modules are not double counted.
    top = prof[~prof["module"].astype(str).str.contains(r"\.")]
    fig, axes = plt.subplots(1, 4, figsize=(15, 3.6))
    for ax, w in zip(axes, WORKLOAD_ORDER):
        m = top[top["workload"] == w].groupby("module")["cumulative_us"].median().nlargest(6) / 1000
        m.sort_values().plot.barh(ax=ax, color="#8172B2")
        ax.set_title(w)
        ax.set_xlabel("cumulative import ms (median)")
        ax.set_ylabel("")
    fig.suptitle(f"Which libraries make INIT slow{t}")
    plt.tight_layout()
    plt.savefig(out / "fig_imports.png", dpi=150)
    plt.close()


def main():
    inv, prof = load()
    t = tag(inv)
    out = ensure_out()
    d = model_a_table(inv)
    summary = function_summary(inv)
    summary.to_csv(out / "summary_by_function.csv")

    arch = fig_arch(d, out, t)
    fig_memory(d, out, t)
    pack = fig_packaging(d, out, t)
    snap = fig_snapstart(summary, out, t)
    fig_imports(prof, out, t)

    print(f"Median INIT (ms) by workload x arch{t}:\n{arch.round(0)}")
    print(f"\narm64 speed-up vs x86: {((1 - arch['arm64'] / arch['x86']) * 100).round(1).to_dict()} %")
    print(f"\n`data` packaging (ms):\n{pack.round(0)}")
    if snap is not None:
        print(f"\nSnapStart:\n{snap.round(0)}")
    print(f"\nwrote figures to {out}")


if __name__ == "__main__":
    pd.set_option("display.width", 120)
    main()
