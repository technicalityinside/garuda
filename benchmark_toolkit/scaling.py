"""
Scaling study: run a workload across a range of thread counts and
report speedup and parallel efficiency.
"""

import dataclasses
from datetime import datetime
from typing import Dict, List, Optional

from .base import BaseWorkload
from .config import BenchmarkConfig, ConfigPreset
from .collector import ResultsCollector
from .runner import BenchmarkRunner, RunResult
from .system import SystemTopology


class ScalingStudy:
    def __init__(
        self,
        runner: BenchmarkRunner,
        collector: ResultsCollector,
        topology: SystemTopology,
    ):
        self.runner = runner
        self.collector = collector
        self.topology = topology

    def run(
        self,
        workload: BaseWorkload,
        thread_counts: Optional[List[int]] = None,
        mode: str = "powers_of_2",
        use_smt: bool = False,
        iterations: int = 1,
        base_config: Optional[BenchmarkConfig] = None,
    ) -> str:
        """
        Run workload across multiple thread counts.

        thread_counts: explicit list of thread counts to test, or None to
                       auto-generate from mode.
        Returns a study_id string that can be passed to print_scaling_table.
        """
        if thread_counts is not None:
            configs = [
                self._make_config(n, use_smt=use_smt, base=base_config)
                for n in thread_counts
            ]
        else:
            configs = ConfigPreset.scaling_configs(
                self.topology,
                mode=mode,
                use_smt=use_smt,
            )

        # Override iterations from parameter
        for cfg in configs:
            cfg.iterations = iterations

        study_ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        study_id = f"scaling_{study_ts}_{workload.name}"

        all_results: List[RunResult] = []
        per_thread: Dict[int, List[RunResult]] = {}

        for cfg in configs:
            print(
                f"  [scaling] threads={cfg.num_threads} cpus={cfg.cpu_list[:4]}"
                f"{'...' if len(cfg.cpu_list) > 4 else ''}"
            )
            results = self.runner.run(workload, cfg)
            per_thread[cfg.num_threads] = results
            all_results.extend(results)

        # Save all results under the study_id
        self.collector.save(all_results, run_id=study_id)

        # Also save per-thread sub-results for detailed access
        for n, results in per_thread.items():
            sub_id = f"{study_id}_t{n}"
            self.collector.save(results, run_id=sub_id)

        return study_id

    def run_named_configs(
        self,
        workload: BaseWorkload,
        config_names: List[str],
        iterations: int = 1,
        workload_args: Optional[Dict] = None,
    ) -> str:
        """
        Run workload with an explicit ordered list of named preset configs.
        Config names can be any preset (e.g. '1c1t', '1c2t', '2c2t', '2c4t').
        Results are keyed by config_name so configs with the same thread count
        are never conflated.
        Returns a study_id.
        """
        import dataclasses as _dc
        presets = ConfigPreset.all_presets(self.topology)

        configs: List[BenchmarkConfig] = []
        for name in config_names:
            if name not in presets:
                available = ", ".join(sorted(presets.keys()))
                raise ValueError(
                    f"Unknown config preset: {name!r}\nAvailable: {available}"
                )
            cfg = _dc.replace(
                presets[name],
                iterations=iterations,
                workload_args=dict(workload_args or {}),
            )
            configs.append(cfg)

        study_ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        study_id = f"scaling_{study_ts}_{workload.name}"

        all_results: List[RunResult] = []
        for cfg in configs:
            print(
                f"  [{cfg.name}] threads={cfg.num_threads}"
                f" cpus={cfg.cpu_list[:4]}{'...' if len(cfg.cpu_list) > 4 else ''}"
            )
            results = self.runner.run(workload, cfg)
            all_results.extend(results)
            self.collector.save(results, run_id=f"{study_id}_{cfg.name}")

        meta = {
            "study_type": "named_configs",
            "config_order": config_names,
        }
        self.collector.save(all_results, run_id=study_id, meta=meta)
        return study_id

    def _make_config(
        self, n: int, use_smt: bool = False, base: Optional[BenchmarkConfig] = None
    ) -> BenchmarkConfig:
        """Create a BenchmarkConfig for n threads, optionally inheriting from base."""
        cpus = self.topology.get_n_cpus(n, socket=0, use_smt=use_smt)

        if base is not None:
            # Copy base config but override thread/cpu fields
            d = dataclasses.asdict(base)
            d["name"] = f"scale_{n}t"
            d["num_threads"] = n
            d["cpu_list"] = cpus
            d["description"] = f"Scaling: {n} thread{'s' if n > 1 else ''}"
            d["scaling_tag"] = f"scale_{n}t"
            return BenchmarkConfig(**d)

        return BenchmarkConfig(
            name=f"scale_{n}t",
            num_threads=n,
            cpu_list=cpus,
            description=f"Scaling: {n} thread{'s' if n > 1 else ''}",
            scaling_tag=f"scale_{n}t",
        )

    def print_scaling_table(self, study_id: str, metric: Optional[str] = None) -> None:
        """
        Load a study and print a scaling table with speedup and efficiency.

        Named-config study:  Config | Threads | <metric> | Speedup | Efficiency
        Thread-count study:  Threads | <metric> | Speedup | Efficiency
        """
        data = self.collector.load(study_id)
        results_list = data.get("results", [])
        meta = data.get("meta", {})

        if not results_list:
            print(f"No results for study {study_id}")
            return

        is_named = meta.get("study_type") == "named_configs"
        config_order: List[str] = meta.get("config_order", [])

        # Auto-pick metric from first successful result
        if metric is None:
            for r in results_list:
                if r.get("success") and r.get("metrics"):
                    metric = next(iter(r["metrics"]))
                    break
        if metric is None:
            print("No successful results with metrics found.")
            return

        workload_name = results_list[0].get("workload", "?")

        if is_named:
            self._print_named_config_table(
                results_list, config_order, metric, workload_name
            )
        else:
            self._print_thread_count_table(results_list, metric, workload_name)

    def _print_named_config_table(
        self,
        results_list: List[dict],
        config_order: List[str],
        metric: str,
        workload_name: str,
    ) -> None:
        """Print scaling table grouped by config name in the original run order."""
        # Group results by config_name
        per_config: Dict[str, List[dict]] = {}
        for r in results_list:
            per_config.setdefault(r.get("config_name", "?"), []).append(r)

        # Compute per-config average
        config_values: Dict[str, float] = {}
        config_threads: Dict[str, int] = {}
        for cfg_name, cfg_results in per_config.items():
            vals = [
                r["metrics"][metric]
                for r in cfg_results
                if r.get("success") and r.get("metrics", {}).get(metric) is not None
            ]
            if vals:
                config_values[cfg_name] = sum(vals) / len(vals)
                config_threads[cfg_name] = cfg_results[0].get("num_threads", 0)

        if not config_values:
            print(f"No results with metric {metric!r}")
            return

        # Baseline: first config in order that has a value
        ordered = [n for n in config_order if n in config_values]
        if not ordered:
            ordered = sorted(config_values.keys(),
                             key=lambda k: config_threads.get(k, 0))
        baseline_val = config_values[ordered[0]]

        print(f"\nScaling study: {workload_name} ({metric})")
        rows = []
        for cfg_name in ordered:
            val = config_values[cfg_name]
            threads = config_threads.get(cfg_name, "?")
            if baseline_val and baseline_val != 0:
                speedup = val / baseline_val
                efficiency = (speedup / threads) * 100.0 if isinstance(threads, int) else float("nan")
            else:
                speedup = efficiency = float("nan")
            rows.append([
                cfg_name,
                str(threads),
                f"{val:.1f}",
                f"{speedup:.2f}x" if not _isnan(speedup) else "N/A",
                f"{efficiency:.0f}%" if not _isnan(efficiency) else "N/A",
            ])

        headers = ["Config", "Threads", metric, "Speedup", "Efficiency"]
        _print_scaling_table(headers, rows)

    def _print_thread_count_table(
        self,
        results_list: List[dict],
        metric: str,
        workload_name: str,
    ) -> None:
        """Print scaling table grouped by thread count."""
        per_thread: Dict[int, List[dict]] = {}
        for r in results_list:
            per_thread.setdefault(r.get("num_threads", 0), []).append(r)

        thread_values: Dict[int, float] = {}
        for t, t_results in per_thread.items():
            vals = [
                r["metrics"][metric]
                for r in t_results
                if r.get("success") and r.get("metrics", {}).get(metric) is not None
            ]
            if vals:
                thread_values[t] = sum(vals) / len(vals)

        if not thread_values:
            print(f"No results with metric {metric!r}")
            return

        speedup_data = self.compute_speedup_from_values(thread_values, metric)
        print(f"\nScaling study: {workload_name} ({metric})")

        rows = []
        for t in sorted(thread_values.keys()):
            val = thread_values[t]
            sp_info = speedup_data.get(t, {})
            speedup = sp_info.get("speedup", float("nan"))
            efficiency = sp_info.get("efficiency", float("nan"))
            rows.append([
                str(t),
                f"{val:.1f}",
                f"{speedup:.2f}x" if not _isnan(speedup) else "N/A",
                f"{efficiency:.0f}%" if not _isnan(efficiency) else "N/A",
            ])

        headers = ["Threads", metric, "Speedup", "Efficiency"]
        _print_scaling_table(headers, rows)

    def compute_speedup(
        self,
        per_thread_results: Dict[int, List[RunResult]],
        metric: str,
    ) -> Dict[int, Dict]:
        """
        Compute speedup and efficiency using T=1 as baseline.
        Returns {threads: {value, speedup, efficiency}}.
        """
        thread_values: Dict[int, float] = {}
        for t, results in per_thread_results.items():
            vals = [
                r.metrics[metric]
                for r in results
                if r.success and metric in r.metrics
            ]
            if vals:
                thread_values[t] = sum(vals) / len(vals)

        return self.compute_speedup_from_values(thread_values, metric)

    def compute_speedup_from_values(
        self, thread_values: Dict[int, float], metric: str
    ) -> Dict[int, Dict]:
        """
        Given {threads: avg_value}, compute speedup and efficiency vs T=1 baseline.
        """
        baseline = thread_values.get(1)
        result: Dict[int, Dict] = {}
        for t, val in thread_values.items():
            if baseline and baseline != 0:
                speedup = val / baseline
                efficiency = (speedup / t) * 100.0
            else:
                speedup = float("nan")
                efficiency = float("nan")
            result[t] = {
                "value": val,
                "speedup": speedup,
                "efficiency": efficiency,
            }
        return result


def _isnan(x) -> bool:
    try:
        import math
        return math.isnan(x)
    except (TypeError, ValueError):
        return True


def _print_scaling_table(headers: List[str], rows: List[List[str]]) -> None:
    """Print a scaling table. Uses tabulate if available, else plain text."""
    try:
        from tabulate import tabulate
        print(tabulate(rows, headers=headers, tablefmt="simple", colalign=("right", "right", "right", "right")))
    except ImportError:
        # Plain text fallback
        col_widths = [len(h) for h in headers]
        for row in rows:
            for i, cell in enumerate(row):
                col_widths[i] = max(col_widths[i], len(str(cell)))

        def fmt_row(cells, right_align=False):
            parts = []
            for i, c in enumerate(cells):
                s = str(c)
                if right_align:
                    parts.append(s.rjust(col_widths[i]))
                else:
                    parts.append(s.ljust(col_widths[i]))
            return " | ".join(parts)

        sep = "-+-".join("-" * w for w in col_widths)
        print(fmt_row(headers))
        print(sep)
        for row in rows:
            print(fmt_row(row, right_align=True))
