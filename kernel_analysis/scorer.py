"""
Composite performance scoring across kernel versions.

Score semantics
---------------
- Baseline kernel (first in the list) = 100.
- Score > 100 means the kernel is *faster* than baseline.
- Score < 100 means it is *slower*.
- Geometric mean is used so that no single metric dominates.

Direction inference
-------------------
Metric names ending in _us / _ms / _ns / _sec / _lat / _latency
are treated as latency (lower is better). Everything else is throughput
(higher is better) — same convention as the regression detector in the portal.
"""

import json
import math
import os
from typing import Dict, List, Optional, Tuple

# Suffixes that mark latency metrics (lower is better)
_LATENCY_SUFFIXES = (
    "_us", "_ms", "_ns", "_sec",
    "_lat", "_latency", "_p50", "_p95", "_p99", "_p999",
    "_avg", "_mean",   # common latency naming conventions
)


def metric_direction(name: str) -> str:
    """Return 'lower' or 'higher' depending on metric semantics."""
    n = name.lower()
    if any(n.endswith(s) for s in _LATENCY_SUFFIXES):
        return "lower"
    return "higher"


def _mean(values: List[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _geomean(values: List[float]) -> float:
    if not values:
        return 0.0
    log_sum = sum(math.log(max(v, 1e-9)) for v in values)
    return math.exp(log_sum / len(values))


# ── Data loading ──────────────────────────────────────────────────────────────

def _load_kernel_data(
    results_dir: str,
    run_ids: List[str],
    workload_filter: Optional[List[str]] = None,
) -> Dict[str, Dict[str, List[float]]]:
    """
    Load results for a list of run_ids and return:
        { workload_name: { metric_name: [values …] } }
    """
    wl_data: Dict[str, Dict[str, List[float]]] = {}

    for run_id in run_ids:
        results_file = os.path.join(results_dir, run_id, "results.json")
        if not os.path.exists(results_file):
            continue
        with open(results_file) as f:
            saved = json.load(f)

        for item in saved.get("results", []):
            wl = item.get("workload", "unknown")
            if workload_filter and wl not in workload_filter:
                continue
            for metric, value in (item.get("metrics") or {}).items():
                try:
                    wl_data.setdefault(wl, {}).setdefault(metric, []).append(float(value))
                except (TypeError, ValueError):
                    pass

    return wl_data


# ── Score computation ─────────────────────────────────────────────────────────

def compute_scores(
    results_dir: str,
    kernel_entries: list,          # List[KernelEntry]
    workloads: Optional[List[str]] = None,
) -> Dict[str, dict]:
    """
    Compute per-kernel composite scores relative to the baseline (first kernel).

    Returns:
        {
          kernel_ver: {
            "score": float,           # composite (baseline=100)
            "workload_scores": {
              workload: {
                "score": float,
                "metrics": {
                  metric: {"mean": float, "score": float, "direction": str}
                }
              }
            }
          }
        }
    """
    # Load data for every kernel that has results
    all_data: Dict[str, Dict] = {}
    for entry in kernel_entries:
        if entry.is_terminal() and entry.run_ids:
            all_data[entry.version] = _load_kernel_data(
                results_dir, entry.run_ids, workloads
            )

    if not all_data:
        return {}

    # Baseline = first kernel in list that has data
    baseline_ver: Optional[str] = None
    for entry in kernel_entries:
        if entry.version in all_data and all_data[entry.version]:
            baseline_ver = entry.version
            break
    if baseline_ver is None:
        return {}

    baseline = all_data[baseline_ver]
    scores: Dict[str, dict] = {}

    for kernel_ver, wl_data in all_data.items():
        wl_scores: Dict[str, dict] = {}

        for wl, metrics in wl_data.items():
            if wl not in baseline:
                continue
            base_metrics = baseline[wl]

            metric_details: Dict[str, dict] = {}
            for metric, values in metrics.items():
                if metric not in base_metrics or not values:
                    continue
                base_val = _mean(base_metrics[metric])
                cur_val = _mean(values)
                if base_val == 0:
                    continue

                direction = metric_direction(metric)
                if direction == "lower":
                    score = (base_val / cur_val) * 100
                else:
                    score = (cur_val / base_val) * 100

                metric_details[metric] = {
                    "mean": cur_val,
                    "score": round(score, 2),
                    "direction": direction,
                }

            if metric_details:
                wl_score = _geomean([v["score"] for v in metric_details.values()])
                wl_scores[wl] = {
                    "score": round(wl_score, 2),
                    "metrics": metric_details,
                }

        composite = _geomean([v["score"] for v in wl_scores.values()]) if wl_scores else 100.0
        scores[kernel_ver] = {
            "score": round(composite, 2),
            "workload_scores": wl_scores,
        }

    return scores


# ── Report printing ───────────────────────────────────────────────────────────

def _delta_str(score: float, baseline_score: float) -> str:
    pct = (score - baseline_score) / baseline_score * 100
    sign = "+" if pct >= 0 else ""
    return f"{sign}{pct:.1f}%"


def print_score_report(kernel_entries: list, scores: Dict[str, dict]) -> None:
    """Print a formatted composite score comparison table."""
    if not scores:
        print("  (no scores computed — no completed results)")
        return

    # Baseline
    baseline_ver: Optional[str] = None
    for entry in kernel_entries:
        if entry.version in scores:
            baseline_ver = entry.version
            break

    W = 72
    print()
    print("=" * W)
    print("  KERNEL ANALYSIS — COMPOSITE SCORES  (baseline = 100)")
    print("=" * W)
    hdr = f"  {'Kernel':<34} {'Score':>7}  {'vs baseline':>12}  {'vs prev':>9}"
    print(hdr)
    print("  " + "-" * (W - 2))

    prev_score: Optional[float] = None
    prev_ver: Optional[str] = None
    for entry in kernel_entries:
        ver = entry.version
        if ver not in scores:
            print(f"  {ver:<34} {'—':>7}  {'—':>12}  {'—':>9}  [{entry.status}]")
            continue

        score = scores[ver]["score"]
        if ver == baseline_ver:
            vs_base = "baseline"
            vs_prev = "—"
        else:
            vs_base = _delta_str(score, scores[baseline_ver]["score"])
            vs_prev = _delta_str(score, prev_score) if prev_score is not None else "—"

        flag = ""
        if ver == baseline_ver:
            flag = "  [baseline]"
        elif score >= 102:
            flag = "  ▲ better"
        elif score <= 98:
            flag = "  ▼ worse"

        print(f"  {ver:<34} {score:>7.1f}  {vs_base:>12}  {vs_prev:>9}{flag}")
        prev_score = score
        prev_ver = ver

    # Per-workload breakdown
    all_workloads: List[str] = []
    for s in scores.values():
        for wl in s["workload_scores"]:
            if wl not in all_workloads:
                all_workloads.append(wl)

    if all_workloads:
        print()
        print("  Per-workload scores (100 = baseline):")
        print("  " + "-" * (W - 2))
        for wl in all_workloads:
            parts = []
            for entry in kernel_entries:
                ver = entry.version
                if ver in scores and wl in scores[ver]["workload_scores"]:
                    s = scores[ver]["workload_scores"][wl]["score"]
                    parts.append(f"{ver}: {s:.1f}")
            print(f"  {wl:<22} {' | '.join(parts)}")

    print("=" * W)

    # Per-workload metric detail
    for wl in all_workloads:
        metrics_shown = set()
        for s in scores.values():
            metrics_shown.update(s["workload_scores"].get(wl, {}).get("metrics", {}).keys())
        if not metrics_shown:
            continue

        print()
        print(f"  {wl} — metric detail:")
        mhdr = f"    {'Metric':<30} {'Dir':>6}  " + "  ".join(
            f"{e.version[:20]:>20}" for e in kernel_entries if e.version in scores
        )
        print(mhdr)
        print("    " + "-" * (W - 4))
        for metric in sorted(metrics_shown):
            direction = metric_direction(metric)
            dir_str = "↓ low" if direction == "lower" else "↑ high"
            values = []
            for entry in kernel_entries:
                ver = entry.version
                if ver not in scores:
                    values.append("—")
                    continue
                mdata = scores[ver]["workload_scores"].get(wl, {}).get("metrics", {})
                if metric in mdata:
                    mean_val = mdata[metric]["mean"]
                    score_val = mdata[metric]["score"]
                    values.append(f"{mean_val:.4g} ({score_val:.0f})")
                else:
                    values.append("—")
            vals_str = "  ".join(f"{v:>20}" for v in values)
            print(f"    {metric:<30} {dir_str:>6}  {vals_str}")
