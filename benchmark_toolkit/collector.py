"""
Results persistence, loading, summarization, and export.

On-disk structure:
  results/
    {run_id}/
      results.json    # list of RunResult dicts + summary stats
"""

import csv
import dataclasses
import json
import math
import os
import statistics
from datetime import datetime
from typing import Dict, List, Optional

from .runner import RunResult


def _result_to_dict(r: RunResult) -> dict:
    return dataclasses.asdict(r)


class ResultsCollector:
    def __init__(self, results_dir: str = "results"):
        self.results_dir = results_dir

    def _ensure_dir(self) -> None:
        os.makedirs(self.results_dir, exist_ok=True)

    def _make_run_id(self, results: List[RunResult]) -> str:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        if results:
            workload = results[0].workload.replace(" ", "_")
            config = results[0].config_name.replace(" ", "_")
            return f"{ts}_{workload}_{config}"
        return ts

    def save(
        self,
        results: List[RunResult],
        run_id: Optional[str] = None,
        meta: Optional[dict] = None,
    ) -> str:
        """Persist results to disk. Returns the run_id used."""
        self._ensure_dir()

        if run_id is None:
            run_id = self._make_run_id(results)

        run_dir = os.path.join(self.results_dir, run_id)
        os.makedirs(run_dir, exist_ok=True)

        summary = self.summarize(results)

        payload = {
            "run_id": run_id,
            "results": [_result_to_dict(r) for r in results],
            "summary": summary,
            "num_results": len(results),
            "num_successful": sum(1 for r in results if r.success),
        }
        if meta:
            payload["meta"] = meta

        out_path = os.path.join(run_dir, "results.json")
        with open(out_path, "w") as f:
            json.dump(payload, f, indent=2)

        return run_id

    def load(self, run_id: str) -> dict:
        """Load results.json for a run_id. Returns the full payload dict."""
        path = os.path.join(self.results_dir, run_id, "results.json")
        if not os.path.isfile(path):
            raise FileNotFoundError(f"No results found for run_id={run_id!r} at {path}")
        with open(path) as f:
            return json.load(f)

    def list_runs(self) -> List[dict]:
        """List all saved runs with summary info."""
        self._ensure_dir()
        runs = []
        for entry in sorted(os.scandir(self.results_dir), key=lambda e: e.name):
            if not entry.is_dir():
                continue
            json_path = os.path.join(entry.path, "results.json")
            if not os.path.isfile(json_path):
                continue
            try:
                with open(json_path) as f:
                    data = json.load(f)
                runs.append({
                    "run_id": entry.name,
                    "num_results": data.get("num_results", 0),
                    "num_successful": data.get("num_successful", 0),
                    "workload": data["results"][0]["workload"] if data.get("results") else "",
                    "config_name": data["results"][0]["config_name"] if data.get("results") else "",
                    "num_threads": data["results"][0]["num_threads"] if data.get("results") else 0,
                    "timestamp": data["results"][0]["timestamp"] if data.get("results") else "",
                })
            except Exception:
                runs.append({"run_id": entry.name, "error": "could not parse results.json"})
        return runs

    def summarize(self, results: List[RunResult]) -> dict:
        """
        For each metric, compute mean, median, stdev, min, max across successful runs.
        Returns {metric_name: {mean, median, stdev, min, max, samples}}.
        """
        successful = [r for r in results if r.success and r.metrics]
        if not successful:
            return {}

        # Collect all metric names
        all_metrics: Dict[str, List[float]] = {}
        for r in successful:
            for k, v in r.metrics.items():
                all_metrics.setdefault(k, []).append(v)

        summary: dict = {}
        for metric, values in all_metrics.items():
            n = len(values)
            mean_val = statistics.mean(values)
            median_val = statistics.median(values)
            stdev_val = statistics.stdev(values) if n > 1 else 0.0
            summary[metric] = {
                "mean": mean_val,
                "median": median_val,
                "stdev": stdev_val,
                "min": min(values),
                "max": max(values),
                "samples": n,
            }

        return summary

    def export_csv(self, run_ids: List[str], output_path: str) -> None:
        """
        Flatten multiple runs to CSV.
        Columns: run_id, workload, config_name, num_threads, metric_name, value, timestamp
        """
        rows = []
        for run_id in run_ids:
            try:
                data = self.load(run_id)
            except FileNotFoundError:
                continue
            for r in data.get("results", []):
                if not r.get("success") or not r.get("metrics"):
                    continue
                for metric_name, value in r["metrics"].items():
                    rows.append({
                        "run_id": run_id,
                        "workload": r.get("workload", ""),
                        "config_name": r.get("config_name", ""),
                        "num_threads": r.get("num_threads", ""),
                        "metric_name": metric_name,
                        "value": value,
                        "timestamp": r.get("timestamp", ""),
                    })

        fieldnames = ["run_id", "workload", "config_name", "num_threads",
                      "metric_name", "value", "timestamp"]
        with open(output_path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)

    def print_summary(self, run_id: str) -> None:
        """Pretty-print summary table for a run."""
        data = self.load(run_id)
        results_list = data.get("results", [])
        summary = data.get("summary", {})

        if not results_list:
            print(f"No results for run {run_id}")
            return

        first = results_list[0]
        workload = first.get("workload", "?")
        config_name = first.get("config_name", "?")
        num_threads = first.get("num_threads", "?")
        n_total = data.get("num_results", len(results_list))
        n_ok = data.get("num_successful", sum(1 for r in results_list if r.get("success")))

        print(f"\nRun: {workload} / {config_name} / {n_total} iteration(s) ({n_ok} successful)")
        print(f"Run ID: {run_id}")

        if not summary:
            print("  (no successful results to summarize)")
            return

        _print_table(summary)


def _print_table(summary: dict) -> None:
    """Print a metrics summary table. Uses tabulate if available, else plain text."""
    if not summary:
        return

    headers = ["Metric", "Mean", "Median", "Stdev", "Min", "Max", "Samples"]
    rows = []
    for metric, stats in sorted(summary.items()):
        rows.append([
            metric,
            f"{stats['mean']:.4g}",
            f"{stats['median']:.4g}",
            f"{stats['stdev']:.4g}",
            f"{stats['min']:.4g}",
            f"{stats['max']:.4g}",
            str(stats['samples']),
        ])

    try:
        from tabulate import tabulate
        print(tabulate(rows, headers=headers, tablefmt="simple"))
    except ImportError:
        # Plain text table fallback
        col_widths = [len(h) for h in headers]
        for row in rows:
            for i, cell in enumerate(row):
                col_widths[i] = max(col_widths[i], len(str(cell)))

        def fmt_row(cells):
            return "  ".join(str(c).ljust(col_widths[i]) for i, c in enumerate(cells))

        print(fmt_row(headers))
        print("  ".join("-" * w for w in col_widths))
        for row in rows:
            print(fmt_row(row))
