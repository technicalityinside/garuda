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

Cloud orchestration:
  cloud-run               Provision a VM, run benchmarks, fetch results, destroy VM
  cloud-provision         Provision a cloud VM (tracked for later use)
  cloud-exec              Run benchmarks on a previously provisioned VM
  cloud-destroy           Destroy a tracked cloud VM
  cloud-list              List tracked cloud VMs
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


# ── Cloud orchestration helpers ──────────────────────────────────────────────

def _build_vm_config(args):
    from orchestrator.base import VMConfig
    return VMConfig(
        provider=args.provider,
        region=args.region,
        instance_type=args.instance_type,
        image=getattr(args, 'image', None),
        disk_size_gb=getattr(args, 'disk_size', 50),
        ssh_user=getattr(args, 'ssh_user', 'ubuntu'),
        ssh_key_path=getattr(args, 'ssh_key_path', '~/.ssh/id_rsa'),
        ssh_key_name=getattr(args, 'aws_key_name', None),
        vm_name=getattr(args, 'vm_name', None),
        zone=getattr(args, 'gcp_zone', None),
        resource_group=getattr(args, 'azure_resource_group', None),
    )


def _provider_kwargs(args) -> dict:
    p = args.provider
    if p == 'gcp':
        return {'project': getattr(args, 'gcp_project', None)}
    if p == 'azure':
        return {'subscription': getattr(args, 'azure_subscription', None)}
    if p == 'aws':
        return {'profile': getattr(args, 'aws_profile', None)}
    return {}


def _build_bench_cmd(args) -> list:
    """Construct the remote 'run' or 'scaling' command from CLI arguments."""
    if getattr(args, 'scaling', False):
        cmd = ['scaling', '--workload', args.workload]
        if getattr(args, 'scaling_configs', None):
            cmd += ['--configs', args.scaling_configs]
        if getattr(args, 'scaling_mode', None):
            cmd += ['--mode', args.scaling_mode]
        if getattr(args, 'threads', None):
            cmd += ['--threads', str(args.threads)]
        if getattr(args, 'max_threads', None):
            cmd += ['--max-threads', str(args.max_threads)]
        if getattr(args, 'smt', False):
            cmd += ['--smt']
    else:
        cmd = ['run', '--workload', args.workload]
        if getattr(args, 'config', None):
            cmd += ['--config', args.config]
        elif getattr(args, 'threads', None):
            cmd += ['--threads', str(args.threads)]

    cmd += ['--iterations', str(getattr(args, 'iterations', 1))]

    for kv in (getattr(args, 'arg', None) or []):
        cmd += ['--arg', kv]

    return cmd


def cmd_cloud_run(args, topo):
    """Provision a VM, run benchmarks, fetch results, then destroy."""
    from orchestrator import get_provider
    from orchestrator.runner import CloudBenchmarkRunner
    from orchestrator.state import VMStateStore

    vm_config = _build_vm_config(args)
    provider = get_provider(args.provider, **_provider_kwargs(args))
    runner = CloudBenchmarkRunner(provider)
    store = VMStateStore()
    bench_cmd = _build_bench_cmd(args)

    def _on_provisioned(instance):
        if args.no_teardown:
            store.save(instance)

    instance = None
    try:
        instance = runner.provision(vm_config, verbose=args.verbose)
        _on_provisioned(instance)
        runner.setup_toolkit(instance, _ROOT, args.verbose)
        runner.run_setup(instance, args.workload, args.verbose)
        runner.run_benchmark(instance, bench_cmd, args.verbose)
        runner.fetch_results(instance, RESULTS_DIR)
    except Exception as exc:
        print(f"\n[cloud] Error: {exc}", file=sys.stderr)
        if instance and not args.no_teardown:
            print("[cloud] Attempting VM cleanup...", file=sys.stderr)
            try:
                runner.destroy(instance)
            except Exception as exc2:
                print(f"[cloud] Cleanup failed: {exc2}", file=sys.stderr)
        sys.exit(1)

    if args.no_teardown:
        print(f"\n[cloud] VM kept running (use 'cloud-destroy --vm-name {instance.name}' to delete it).")
    else:
        runner.destroy(instance)
        if args.no_teardown:
            store.remove(instance.name)


def cmd_cloud_provision(args, topo):
    """Provision a cloud VM and save its info for later use."""
    from orchestrator import get_provider
    from orchestrator.runner import CloudBenchmarkRunner
    from orchestrator.state import VMStateStore

    vm_config = _build_vm_config(args)
    provider = get_provider(args.provider, **_provider_kwargs(args))
    runner = CloudBenchmarkRunner(provider)
    store = VMStateStore()

    instance = runner.provision(vm_config, verbose=getattr(args, 'verbose', False))
    store.save(instance)

    print(f"\nVM '{instance.name}' is ready.")
    print(f"  Provider:  {instance.provider}")
    print(f"  Type:      {instance.instance_type}")
    print(f"  IP:        {instance.public_ip}")
    print(f"  SSH:       ssh -i {instance.ssh_key_path} {instance.ssh_user}@{instance.public_ip}")
    print(f"\n  Run bench: python3 main.py cloud-exec --vm-name {instance.name} --workload <name>")
    print(f"  Destroy:   python3 main.py cloud-destroy --vm-name {instance.name}")


def cmd_cloud_exec(args, topo):
    """Run benchmarks on a previously provisioned VM."""
    from orchestrator import get_provider
    from orchestrator.runner import CloudBenchmarkRunner
    from orchestrator.state import VMStateStore

    store = VMStateStore()
    instance = store.get(args.vm_name)
    if instance is None:
        print(f"Error: VM '{args.vm_name}' not found. Run 'cloud-list' to see tracked VMs.")
        sys.exit(1)

    provider = get_provider(instance.provider)
    runner = CloudBenchmarkRunner(provider)
    bench_cmd = _build_bench_cmd(args)

    if not getattr(args, 'skip_setup', False):
        runner.setup_toolkit(instance, _ROOT, getattr(args, 'verbose', False))
        runner.run_setup(instance, args.workload, getattr(args, 'verbose', False))

    rc = runner.run_benchmark(instance, bench_cmd, getattr(args, 'verbose', False))
    runner.fetch_results(instance, RESULTS_DIR)
    if rc != 0:
        sys.exit(rc)


def cmd_cloud_destroy(args, topo):
    """Destroy a tracked cloud VM."""
    from orchestrator import get_provider
    from orchestrator.runner import CloudBenchmarkRunner
    from orchestrator.state import VMStateStore

    store = VMStateStore()
    instance = store.get(args.vm_name)
    if instance is None:
        print(f"Error: VM '{args.vm_name}' not found in state. "
              "It may already have been destroyed or was created with --no-teardown skipped.")
        sys.exit(1)

    provider = get_provider(instance.provider)
    runner = CloudBenchmarkRunner(provider)
    runner.destroy(instance)
    store.remove(instance.name)
    print(f"VM '{instance.name}' destroyed and removed from state.")


def cmd_cloud_list(args, topo):
    """List cloud VMs tracked by this toolkit."""
    from orchestrator.state import VMStateStore

    store = VMStateStore()
    vms = store.list_all()
    if not vms:
        print("No tracked VMs. Use 'cloud-provision' or 'cloud-run --no-teardown' to create one.")
        return

    headers = ["Name", "Provider", "Type", "Region", "IP", "State"]
    rows = [
        [v.name, v.provider, v.instance_type, v.region, v.public_ip, v.state]
        for v in vms
    ]
    _print_simple_table(headers, rows)
    print(f"\n{len(vms)} tracked VM(s).  State shown is from provisioning time.")


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

    # ── Cloud orchestration subcommands ──────────────────────────────────────

    def _add_vm_args(p):
        """Attach common cloud VM arguments to a subparser."""
        g = p.add_argument_group("VM configuration")
        g.add_argument("--provider", required=True, choices=["gcp", "azure", "aws"],
                       help="Cloud provider")
        g.add_argument("--region", required=True,
                       help="Region (e.g. us-central1, eastus, us-east-1)")
        g.add_argument("--instance-type", required=True,
                       help="Instance type (e.g. n2-standard-4, Standard_D4s_v3, m5.xlarge)")
        g.add_argument("--ssh-key-path", default="~/.ssh/id_rsa", metavar="PATH",
                       help="Local SSH private key (default: ~/.ssh/id_rsa)")
        g.add_argument("--ssh-user", default="ubuntu", metavar="USER",
                       help="SSH username on the VM (default: ubuntu)")
        g.add_argument("--vm-name", metavar="NAME",
                       help="Custom VM name (auto-generated if omitted)")
        g.add_argument("--disk-size", type=int, default=50, metavar="GB",
                       help="Boot disk size in GB (default: 50)")
        g.add_argument("--image", metavar="IMAGE",
                       help="OS image override (default: Ubuntu 22.04 LTS)")
        g.add_argument("--gcp-zone",
                       help="GCP zone override (default: {region}-a)")
        g.add_argument("--gcp-project",
                       help="GCP project ID (uses gcloud default if omitted)")
        g.add_argument("--aws-key-name", metavar="KEYPAIR",
                       help="AWS EC2 key-pair name (required for AWS)")
        g.add_argument("--aws-profile",
                       help="AWS CLI named profile")
        g.add_argument("--azure-resource-group", metavar="RG",
                       help="Azure resource group (auto-created if omitted)")
        g.add_argument("--azure-subscription",
                       help="Azure subscription ID")

    def _add_bench_args(p, workload_required=True):
        """Attach benchmark selection arguments to a subparser."""
        g = p.add_argument_group("Benchmark")
        g.add_argument("--workload", required=workload_required, metavar="NAME",
                       help="Workload to run (see list-workloads)")
        g.add_argument("--scaling", action="store_true",
                       help="Run a scaling study instead of a single benchmark run")
        g.add_argument("--config", metavar="PRESET",
                       help="Config preset for single run (e.g. full_socket, 4c4t)")
        g.add_argument("--threads", metavar="N",
                       help="Thread count for single run, or comma-separated list for scaling")
        g.add_argument("--iterations", type=int, default=1, metavar="N",
                       help="Iterations per config (default: 1)")
        g.add_argument("--scaling-configs", metavar="1c1t,2c2t,...",
                       help="Named config presets for scaling study")
        g.add_argument("--scaling-mode", choices=["powers_of_2", "linear"],
                       default="powers_of_2",
                       help="Thread sweep mode for scaling (default: powers_of_2)")
        g.add_argument("--max-threads", type=int, metavar="N",
                       help="Max threads for scaling sweep")
        g.add_argument("--smt", action="store_true",
                       help="Include SMT/HT siblings in scaling study")
        g.add_argument("--arg", nargs="*", metavar="key=value",
                       help="Workload-specific arguments (e.g. --arg time=30 prime=50000)")

    # cloud-run
    p_crun = subparsers.add_parser(
        "cloud-run",
        help="Provision a VM, run benchmarks, fetch results, then destroy the VM",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            Examples:
              # GCP — single run
              python main.py cloud-run --provider gcp --region us-central1 \\
                --instance-type n2-standard-4 --workload python_bench --config full_socket

              # AWS — scaling study, keep VM afterwards
              python main.py cloud-run --provider aws --region us-east-1 \\
                --instance-type m5.xlarge --aws-key-name my-keypair \\
                --workload sysbench_cpu --scaling --max-threads 16 --no-teardown

              # Azure — custom iterations
              python main.py cloud-run --provider azure --region eastus \\
                --instance-type Standard_D4s_v3 --workload stream --iterations 3
        """),
    )
    _add_vm_args(p_crun)
    _add_bench_args(p_crun)
    p_crun.add_argument("--no-teardown", action="store_true",
                        help="Keep VM running after benchmark (tracked by cloud-list)")
    p_crun.add_argument("--verbose", action="store_true",
                        help="Print detailed output from remote commands")

    # cloud-provision
    p_cprov = subparsers.add_parser(
        "cloud-provision",
        help="Provision a cloud VM and save it for later use with cloud-exec",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            Examples:
              python main.py cloud-provision --provider gcp --region us-central1 \\
                --instance-type n2-standard-4
              python main.py cloud-provision --provider aws --region us-east-1 \\
                --instance-type m5.xlarge --aws-key-name my-keypair
        """),
    )
    _add_vm_args(p_cprov)
    p_cprov.add_argument("--verbose", action="store_true")

    # cloud-exec
    p_cexec = subparsers.add_parser(
        "cloud-exec",
        help="Run benchmarks on a VM that was provisioned with cloud-provision",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            Examples:
              python main.py cloud-exec --vm-name benchmark-abc12345 \\
                --workload python_bench --config full_socket --iterations 3
              python main.py cloud-exec --vm-name benchmark-abc12345 \\
                --workload sysbench_cpu --scaling --skip-setup
        """),
    )
    p_cexec.add_argument("--vm-name", required=True, metavar="NAME",
                         help="Name of a VM from 'cloud-list'")
    p_cexec.add_argument("--skip-setup", action="store_true",
                         help="Skip toolkit sync and workload setup (VM already prepared)")
    p_cexec.add_argument("--verbose", action="store_true")
    _add_bench_args(p_cexec)

    # cloud-destroy
    p_cdest = subparsers.add_parser(
        "cloud-destroy",
        help="Destroy a tracked cloud VM",
    )
    p_cdest.add_argument("--vm-name", required=True, metavar="NAME",
                         help="Name of the VM to destroy (from cloud-list)")

    # cloud-list
    subparsers.add_parser(
        "cloud-list",
        help="List cloud VMs tracked by this toolkit",
    )

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
        "cloud-run": cmd_cloud_run,
        "cloud-provision": cmd_cloud_provision,
        "cloud-exec": cmd_cloud_exec,
        "cloud-destroy": cmd_cloud_destroy,
        "cloud-list": cmd_cloud_list,
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
