#!/usr/bin/env python3
"""
main.py - CLI entry point for the benchmark toolkit.

Subcommands:
  list-workloads          List all discovered workloads
  list-configs            List all config presets for the current system
  validate                Validate workload dependencies
  run                     Run a workload with a given config
  scaling                 Run a scaling study
  report                  Show or export saved results
"""

import argparse
import os
import sys
import textwrap
from typing import Dict, List, Optional

# Ensure the toolkit root is on sys.path
_ROOT = os.path.dirname(os.path.abspath(__file__))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from benchmark_toolkit.system import detect_topology
from benchmark_toolkit.config import BenchmarkConfig, ConfigPreset
from benchmark_toolkit.registry import registry
from benchmark_toolkit.runner import BenchmarkRunner, RunResult
from benchmark_toolkit.collector import ResultsCollector, _print_table
from benchmark_toolkit.scaling import ScalingStudy

WORKLOADS_DIR = os.path.join(_ROOT, "workloads")
RESULTS_DIR   = os.path.join(_ROOT, "results")
BIN_DIR       = os.path.join(_ROOT, "bin")


def _discover_workloads():
    """Import workloads directory and register all workloads."""
    registry.discover(WORKLOADS_DIR)


def _prepend_bin_dir():
    """Add the local bin/ directory to PATH so locally installed binaries are found."""
    if os.path.isdir(BIN_DIR):
        os.environ["PATH"] = BIN_DIR + os.pathsep + os.environ.get("PATH", "")


def _get_topology():
    topo = detect_topology()
    return topo


def _parse_workload_args(arg_list: Optional[List[str]]) -> Dict:
    """Parse list of 'key=value' strings into a dict."""
    result = {}
    if not arg_list:
        return result
    for item in arg_list:
        if "=" not in item:
            print(f"[WARNING] Ignoring malformed workload arg (expected key=value): {item!r}")
            continue
        key, _, value = item.partition("=")
        # Try int, then float, then string
        for cast in (int, float):
            try:
                result[key.strip()] = cast(value.strip())
                break
            except ValueError:
                pass
        else:
            result[key.strip()] = value.strip()
    return result


def cmd_list_workloads(args, topo):
    """List all discovered workloads."""
    workloads = registry.list_all()
    if not workloads:
        print("No workloads discovered. Check the workloads/ directory.")
        return

    print(f"\nDiscovered {len(workloads)} workload(s):\n")
    rows = [[w["name"], w["version"], w["description"]] for w in workloads]
    headers = ["Name", "Version", "Description"]
    _print_simple_table(headers, rows)


def cmd_list_configs(args, topo):
    """List all config presets for the current system."""
    presets = ConfigPreset.all_presets(topo)
    print(f"\nSystem: {topo.total_physical_cores} physical cores, "
          f"{topo.total_logical_cpus} logical CPUs, "
          f"{topo.total_sockets} socket(s), "
          f"{topo.total_numa_nodes} NUMA node(s)\n")

    rows = []
    for name, cfg in sorted(presets.items()):
        cpu_preview = str(cfg.cpu_list[:4])
        if len(cfg.cpu_list) > 4:
            cpu_preview = cpu_preview[:-1] + ", ...]"
        rows.append([name, str(cfg.num_threads), cpu_preview, cfg.description])

    headers = ["Preset", "Threads", "CPUs (preview)", "Description"]
    _print_simple_table(headers, rows)


def cmd_validate(args, topo):
    """Validate workload dependencies."""
    names = [args.workload] if args.workload else [w["name"] for w in registry.list_all()]
    if not names:
        print("No workloads to validate.")
        return

    print(f"\nValidating {len(names)} workload(s):\n")
    all_ok = True
    rows = []
    for name in names:
        try:
            wl = registry.get(name)
            ok, msg = wl.validate()
        except KeyError as e:
            ok = False
            msg = str(e)
        status = "OK" if ok else "MISSING"
        if not ok:
            all_ok = False
        rows.append([name, status, msg])

    headers = ["Workload", "Status", "Message"]
    _print_simple_table(headers, rows)
    if not all_ok:
        sys.exit(1)


def cmd_run(args, topo):
    """Run a workload."""
    # Resolve workload
    try:
        workload = registry.get(args.workload)
    except KeyError as e:
        print(f"Error: {e}")
        sys.exit(1)

    # Skip validation for dry-run (binary may not be installed on this machine)
    if not args.dry_run:
        ok, msg = workload.validate()
        if not ok:
            print(f"Validation failed for {args.workload}: {msg}")
            sys.exit(1)

    # Build config
    workload_args = _parse_workload_args(args.arg)

    if args.config:
        presets = ConfigPreset.all_presets(topo)
        if args.config not in presets:
            print(f"Unknown preset: {args.config!r}")
            print(f"Available presets: {', '.join(sorted(presets))}")
            sys.exit(1)
        config = presets[args.config]
        config.iterations = args.iterations
        config.workload_args = workload_args
    elif args.threads is not None:
        cpu_list = topo.get_n_cpus(args.threads)
        config = BenchmarkConfig(
            name=f"custom_{args.threads}t",
            num_threads=args.threads,
            cpu_list=cpu_list,
            iterations=args.iterations,
            description=f"Custom: {args.threads} threads",
            workload_args=workload_args,
        )
    else:
        # Default: single_core
        config = ConfigPreset.single_core(topo)
        config.iterations = args.iterations
        config.workload_args = workload_args
        print(f"No --config or --threads specified, defaulting to single_core preset.")

    runner = BenchmarkRunner(
        work_dir=args.work_dir,
        dry_run=args.dry_run,
        verbose=args.verbose,
        bin_dir=BIN_DIR,
    )
    collector = ResultsCollector(results_dir=RESULTS_DIR)

    print(f"\nRunning: {workload.name} / {config.name} / {config.iterations} iteration(s)")
    print(f"  Threads: {config.num_threads}  CPUs: {config.cpu_list}")
    if args.dry_run:
        print("  [DRY RUN MODE]")
    print()

    results = runner.run(workload, config)

    # Print per-iteration summary if not verbose (verbose already printed them)
    if not args.verbose and not args.dry_run:
        for r in results:
            status = "OK" if r.success else "FAIL"
            print(f"  iter={r.iteration} [{status}] wall={r.wall_time:.2f}s", end="")
            if r.success and r.metrics:
                parts = [f"{k}={v:.4g}" for k, v in r.metrics.items()]
                print(f"  {', '.join(parts)}")
            elif r.error_msg:
                print(f"  {r.error_msg}")
            else:
                print()

    if args.dry_run:
        return

    # Save results
    run_id = collector.save(results)
    summary = collector.summarize(results)

    # Print summary table
    n_ok = sum(1 for r in results if r.success)
    print(f"\nRun: {workload.name} / {config.name} / {config.iterations} iteration(s)")
    print(f"  {n_ok}/{config.iterations} successful  |  run_id: {run_id}")

    if summary:
        print()
        _print_run_summary_table(workload.name, config.name, config.iterations, summary)
    else:
        print("  (no successful results)")

    print(f"\nResults saved to: {RESULTS_DIR}/{run_id}/results.json")


def cmd_scaling(args, topo):
    """Run a scaling study."""
    try:
        workload = registry.get(args.workload)
    except KeyError as e:
        print(f"Error: {e}")
        sys.exit(1)

    ok, msg = (True, "") if args.dry_run else workload.validate()
    if not ok:
        print(f"Validation failed for {args.workload}: {msg}")
        sys.exit(1)

    workload_args = _parse_workload_args(getattr(args, "arg", None))

    runner = BenchmarkRunner(
        work_dir=args.work_dir,
        dry_run=args.dry_run,
        verbose=args.verbose,
        bin_dir=BIN_DIR,
    )
    collector = ResultsCollector(results_dir=RESULTS_DIR)
    study = ScalingStudy(runner=runner, collector=collector, topology=topo)

    named_configs = None
    if getattr(args, "configs", None):
        named_configs = [s.strip() for s in args.configs.split(",") if s.strip()]

    max_threads = args.max_threads
    thread_counts = None
    if not named_configs and args.threads:
        try:
            thread_counts = [int(x) for x in args.threads.split(",")]
        except ValueError:
            print(f"Error: --threads must be comma-separated integers, got {args.threads!r}")
            sys.exit(1)

    print(f"\nScaling study: {workload.name}")
    if named_configs:
        print(f"  configs={named_configs}  iterations={args.iterations}")
    else:
        print(f"  mode={args.mode}  smt={args.smt}  iterations={args.iterations}")
        if thread_counts:
            print(f"  thread_counts={thread_counts}")
        elif max_threads:
            print(f"  max_threads={max_threads}")
    if workload_args:
        print(f"  workload_args={workload_args}")
    print()

    if named_configs:
        try:
            study_id = study.run_named_configs(
                workload=workload,
                config_names=named_configs,
                iterations=args.iterations,
                workload_args=workload_args or None,
            )
        except ValueError as e:
            print(f"Error: {e}")
            sys.exit(1)
    else:
        base_config = None
        if workload_args:
            base_config = BenchmarkConfig(
                name="scaling_base",
                num_threads=1,
                cpu_list=[0],
                workload_args=workload_args,
            )
        study_id = study.run(
            workload=workload,
            thread_counts=thread_counts,
            mode=args.mode,
            use_smt=args.smt,
            iterations=args.iterations,
            base_config=base_config,
        )

    if not args.dry_run:
        print(f"\nStudy ID: {study_id}")
        study.print_scaling_table(study_id)
        print(f"\nResults saved to: {RESULTS_DIR}/{study_id}/")


def cmd_setup(args, topo):
    """Download, build, and install benchmark binaries."""
    install_dir = args.install_dir or BIN_DIR
    os.makedirs(install_dir, exist_ok=True)

    names = (
        [args.workload]
        if args.workload
        else [w["name"] for w in registry.list_all()]
    )

    # ── status-only listing ──────────────────────────────────────────────────
    if args.list:
        print(f"\nWorkload setup status  (local bin dir: {install_dir})\n")
        rows = []
        for name in names:
            try:
                wl = registry.get(name)
                ok, msg = wl.validate()
                rows.append([name, "OK" if ok else "MISSING",
                              wl.install_hint, msg])
            except KeyError as exc:
                rows.append([name, "ERROR", "?", str(exc)])
        _print_simple_table(["Workload", "Status", "Install method", "Message"], rows)
        return

    # ── install ──────────────────────────────────────────────────────────────
    all_ok = True
    for name in names:
        try:
            wl = registry.get(name)
        except KeyError as exc:
            print(f"\n[ERROR] Unknown workload: {exc}")
            all_ok = False
            continue

        ok, msg = wl.validate()
        print(f"\nSetup: {wl.name}  [{wl.install_hint}]")
        print(f"  Status : {'OK' if ok else 'MISSING'} — {msg}")

        if ok and not args.force:
            print("  Skipping (already installed). Use --force to reinstall.")
            continue

        if wl.install_hint == "no install needed":
            print("  Nothing to install.")
            continue

        print(f"  Target : {install_dir}")
        install_ok, install_msg = wl.install(install_dir, force=args.force)
        if install_ok:
            print(f"  [OK]     {install_msg}")
        else:
            print(f"  [FAILED] {install_msg}")
            all_ok = False

    print()
    if all_ok:
        print("Run 'python3 main.py validate' to confirm all workloads are ready.")
    else:
        sys.exit(1)


def cmd_report(args, topo):
    """Show or export saved results."""
    collector = ResultsCollector(results_dir=RESULTS_DIR)

    if args.list:
        runs = collector.list_runs()
        if not runs:
            print("No saved runs found.")
            return
        print(f"\nSaved runs ({len(runs)}):\n")
        rows = []
        for r in runs:
            if "error" in r:
                rows.append([r["run_id"], "?", "?", "?", r["error"]])
            else:
                rows.append([
                    r["run_id"],
                    r.get("workload", "?"),
                    r.get("config_name", "?"),
                    str(r.get("num_threads", "?")),
                    f"{r.get('num_successful', 0)}/{r.get('num_results', 0)}",
                ])
        headers = ["Run ID", "Workload", "Config", "Threads", "Success"]
        _print_simple_table(headers, rows)
        return

    if args.csv:
        if not args.run_id:
            # Export all runs
            runs = collector.list_runs()
            run_ids = [r["run_id"] for r in runs if "error" not in r]
        else:
            run_ids = [args.run_id]
        collector.export_csv(run_ids, args.csv)
        print(f"Exported {len(run_ids)} run(s) to {args.csv}")
        return

    if args.run_id:
        collector.print_summary(args.run_id)
    else:
        # Show most recent run
        runs = collector.list_runs()
        if not runs:
            print("No saved runs. Use 'run' or 'scaling' subcommands first.")
            return
        most_recent = runs[-1]
        print(f"Showing most recent run: {most_recent['run_id']}")
        collector.print_summary(most_recent["run_id"])


# ── Table printing helpers ──────────────────────────────────────────────────

def _print_simple_table(headers: List[str], rows: List[List[str]]) -> None:
    """Print a simple table. Uses tabulate if available, else plain text."""
    try:
        from tabulate import tabulate
        print(tabulate(rows, headers=headers, tablefmt="simple"))
    except ImportError:
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


def _print_run_summary_table(workload: str, config: str, iterations: int, summary: dict) -> None:
    """Print a formatted run summary table."""
    print(f"Run: {workload} / {config} / {iterations} iteration(s)")

    headers = ["Metric", "Mean", "Median", "Stdev", "Min", "Max"]
    rows = []
    for metric, stats in sorted(summary.items()):
        rows.append([
            metric,
            f"{stats['mean']:.4g}",
            f"{stats['median']:.4g}",
            f"{stats['stdev']:.4g}",
            f"{stats['min']:.4g}",
            f"{stats['max']:.4g}",
        ])

    try:
        from tabulate import tabulate
        print(tabulate(rows, headers=headers, tablefmt="grid"))
    except ImportError:
        # Unicode box-drawing fallback
        col_widths = [len(h) for h in headers]
        for row in rows:
            for i, cell in enumerate(row):
                col_widths[i] = max(col_widths[i], len(str(cell)))

        def _hline(left, mid, right, fill):
            parts = [fill * (w + 2) for w in col_widths]
            return left + mid.join(parts) + right

        def _data_row(cells):
            parts = [f" {str(c).ljust(col_widths[i])} " for i, c in enumerate(cells)]
            return "│" + "│".join(parts) + "│"

        top = _hline("┌", "┬", "┐", "─")
        mid = _hline("├", "┼", "┤", "─")
        bot = _hline("└", "┴", "┘", "─")

        print(top)
        print(_data_row(headers))
        print(mid)
        for row in rows:
            print(_data_row(row))
        print(bot)


# ── Argument parser ──────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="benchmark_toolkit",
        description="Python workload automation toolkit",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            Examples:
              python main.py setup --list
              python main.py setup --workload multichase
              python main.py setup --workload stream --install-dir ./bin
              python main.py setup                        # install all missing workloads
              python main.py list-workloads
              python main.py list-configs
              python main.py validate
              python main.py validate --workload sysbench_cpu
              python main.py run --workload python_bench --config full_socket --iterations 3
              python main.py run --workload sysbench_cpu --threads 8 --arg time=30 prime=50000
              python main.py scaling --workload python_bench --mode powers_of_2
              python main.py scaling --workload python_bench --threads 1,2,4,8,16
              python main.py report --list
              python main.py report --run-id 20240508_143022_python_bench_full_socket
              python main.py report --csv output.csv
        """),
    )

    parser.add_argument(
        "--work-dir",
        default="/tmp/benchmark_toolkit",
        help="Working directory for temp files (default: /tmp/benchmark_toolkit)",
    )

    subparsers = parser.add_subparsers(dest="command", metavar="COMMAND")
    subparsers.required = True

    # setup
    p_setup = subparsers.add_parser(
        "setup", help="Download, build, and install benchmark binaries"
    )
    p_setup.add_argument(
        "--workload", metavar="NAME",
        help="Install a specific workload only (default: all missing workloads)",
    )
    p_setup.add_argument(
        "--install-dir", metavar="DIR",
        help=f"Directory for compiled/installed binaries (default: {BIN_DIR})",
    )
    p_setup.add_argument(
        "--force", action="store_true",
        help="Reinstall even if the binary is already present",
    )
    p_setup.add_argument(
        "--list", action="store_true",
        help="Show install status and methods without installing anything",
    )

    # list-workloads
    subparsers.add_parser("list-workloads", help="List all discovered workloads")

    # list-configs
    subparsers.add_parser("list-configs", help="List all config presets")

    # validate
    p_val = subparsers.add_parser("validate", help="Validate workload dependencies")
    p_val.add_argument("--workload", metavar="NAME", help="Validate a specific workload only")

    # run
    p_run = subparsers.add_parser("run", help="Run a workload benchmark")
    p_run.add_argument("--workload", required=True, metavar="NAME", help="Workload name")
    p_run.add_argument(
        "--config", metavar="PRESET",
        help="Config preset in NcMT form (e.g. 1c1t, 1c2t, 2c2t, 4c4t, 4c8t, "
             "16c16t, 16c32t) or legacy alias (single_core, full_socket, ...); "
             "see list-configs for all options on this machine",
    )
    p_run.add_argument(
        "--threads", type=int, metavar="N",
        help="Number of threads (alternative to --config)",
    )
    p_run.add_argument(
        "--iterations", type=int, default=1, metavar="N",
        help="Number of iterations (default: 1)",
    )
    p_run.add_argument("--dry-run", action="store_true", help="Print command without executing")
    p_run.add_argument("--verbose", action="store_true", help="Print per-iteration results")
    p_run.add_argument(
        "--arg", nargs="*", metavar="key=value",
        help="Workload-specific arguments, e.g. --arg time=30 prime=50000",
    )

    # scaling
    p_scale = subparsers.add_parser("scaling", help="Run a scaling study")
    p_scale.add_argument("--workload", required=True, metavar="NAME", help="Workload name")
    p_scale.add_argument(
        "--mode", choices=["powers_of_2", "linear"], default="powers_of_2",
        help="Thread count generation mode (default: powers_of_2)",
    )
    p_scale.add_argument(
        "--max-threads", type=int, metavar="N",
        help="Maximum thread count",
    )
    p_scale.add_argument(
        "--threads", metavar="1,2,4,...",
        help="Explicit comma-separated thread counts (overrides --mode)",
    )
    p_scale.add_argument(
        "--configs", metavar="1c1t,1c2t,...",
        help="Comma-separated list of named config presets to compare "
             "(e.g. 1c1t,1c2t,2c2t,2c4t); overrides --mode and --threads",
    )
    p_scale.add_argument("--smt", action="store_true", help="Include SMT/HT siblings")
    p_scale.add_argument(
        "--iterations", type=int, default=1, metavar="N",
        help="Iterations per thread count (default: 1)",
    )
    p_scale.add_argument("--dry-run", action="store_true", help="Print commands without executing")
    p_scale.add_argument("--verbose", action="store_true", help="Print per-iteration results")
    p_scale.add_argument(
        "--arg", nargs="*", metavar="key=value",
        help="Workload-specific arguments passed to every config, e.g. --arg duration=5",
    )

    # report
    p_report = subparsers.add_parser("report", help="Show or export saved results")
    p_report.add_argument("--run-id", metavar="ID", help="Run ID to display")
    p_report.add_argument("--list", action="store_true", help="List all saved runs")
    p_report.add_argument("--csv", metavar="FILE", help="Export results to CSV file")

    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()

    # Add missing attributes for subcommands that don't define them
    if not hasattr(args, "dry_run"):
        args.dry_run = False
    if not hasattr(args, "verbose"):
        args.verbose = False
    if not hasattr(args, "work_dir") or args.work_dir is None:
        args.work_dir = "/tmp/benchmark_toolkit"

    # Prepend local bin/ to PATH so locally installed binaries are found by validate()
    _prepend_bin_dir()

    # Discover workloads
    _discover_workloads()

    # Detect topology
    try:
        topo = _get_topology()
    except Exception as e:
        print(f"[WARNING] Could not detect CPU topology: {e}")
        # Create a minimal fallback topology
        from benchmark_toolkit.system import SystemTopology, CoreInfo
        topo = SystemTopology(
            total_logical_cpus=1,
            total_physical_cores=1,
            total_sockets=1,
            total_numa_nodes=1,
            cores=[CoreInfo(core_id=0, socket_id=0, numa_node=0, logical_cpus=[0])],
            socket_cpus={0: [0]},
            socket_physical_cpus={0: [0]},
            numa_cpus={0: [0]},
        )

    dispatch = {
        "setup": cmd_setup,
        "list-workloads": cmd_list_workloads,
        "list-configs": cmd_list_configs,
        "validate": cmd_validate,
        "run": cmd_run,
        "scaling": cmd_scaling,
        "report": cmd_report,
    }

    handler = dispatch.get(args.command)
    if handler is None:
        parser.print_help()
        sys.exit(1)

    try:
        handler(args, topo)
    except KeyboardInterrupt:
        print("\nInterrupted.")
        sys.exit(130)


if __name__ == "__main__":
    main()
