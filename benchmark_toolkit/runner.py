"""
Benchmark runner: wraps subprocess execution with taskset/numactl pinning,
timeout handling, and per-iteration result recording.
"""

import os
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional

from .base import BaseWorkload
from .config import BenchmarkConfig


@dataclass
class RunResult:
    workload: str
    config_name: str
    num_threads: int
    cpu_list: List[int]
    metrics: Dict[str, float]
    raw_stdout: str
    raw_stderr: str
    returncode: int
    wall_time: float       # measured with time.perf_counter
    iteration: int
    timestamp: str         # ISO format
    success: bool
    error_msg: str = ""
    command: List[str] = field(default_factory=list)


class BenchmarkRunner:
    def __init__(
        self,
        work_dir: str = "/tmp/benchmark_toolkit",
        dry_run: bool = False,
        verbose: bool = False,
        bin_dir: Optional[str] = None,
    ):
        self.work_dir = work_dir
        self.dry_run = dry_run
        self.verbose = verbose
        self.bin_dir = bin_dir  # prepended to PATH for every subprocess

    def run(
        self, workload: BaseWorkload, config: BenchmarkConfig
    ) -> List[RunResult]:
        """
        Run workload config.iterations times.
        Calls setup once before all iterations and teardown once after.
        Returns list of RunResult (one per iteration).
        """
        os.makedirs(self.work_dir, exist_ok=True)

        if not self.dry_run:
            try:
                workload.setup(config, self.work_dir)
            except Exception as exc:
                # Setup failure: return a single failed result
                return [
                    RunResult(
                        workload=workload.name,
                        config_name=config.name,
                        num_threads=config.num_threads,
                        cpu_list=list(config.cpu_list),
                        metrics={},
                        raw_stdout="",
                        raw_stderr="",
                        returncode=-1,
                        wall_time=0.0,
                        iteration=0,
                        timestamp=datetime.now().isoformat(),
                        success=False,
                        error_msg=f"Setup failed: {exc}",
                        command=[],
                    )
                ]

        results: List[RunResult] = []
        for i in range(config.iterations):
            r = self._run_once(workload, config, i)
            results.append(r)
            if self.verbose:
                self._print_result(r)

        if not self.dry_run:
            try:
                workload.teardown(config, self.work_dir)
            except Exception as exc:
                if self.verbose:
                    print(f"[WARNING] teardown failed: {exc}")

        return results

    def _run_once(
        self, workload: BaseWorkload, config: BenchmarkConfig, iteration: int
    ) -> RunResult:
        cmd = workload.build_command(config)

        # Prepend taskset if requested
        if config.use_taskset and config.cpu_list:
            cpu_str = ",".join(map(str, config.cpu_list))
            cmd = ["taskset", "-c", cpu_str] + cmd

        # Prepend numactl if requested (after taskset)
        if config.use_numactl and config.numa_nodes:
            numa_str = ",".join(map(str, config.numa_nodes))
            cmd = [
                "numactl",
                f"--cpunodebind={numa_str}",
                f"--membind={numa_str}",
            ] + cmd

        env = {**os.environ, **workload.get_env(config)}
        if self.bin_dir and os.path.isdir(self.bin_dir):
            env["PATH"] = self.bin_dir + os.pathsep + env.get("PATH", "")

        if self.dry_run:
            print(f"[DRY RUN] {' '.join(cmd)}")
            return RunResult(
                workload=workload.name,
                config_name=config.name,
                num_threads=config.num_threads,
                cpu_list=list(config.cpu_list),
                metrics={},
                raw_stdout="",
                raw_stderr="",
                returncode=0,
                wall_time=0.0,
                iteration=iteration,
                timestamp=datetime.now().isoformat(),
                success=True,
                error_msg="",
                command=cmd,
            )

        # Execute
        stdout = ""
        stderr = ""
        returncode = -1
        elapsed = 0.0
        error_msg = ""

        try:
            start = time.perf_counter()
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                env=env,
                timeout=config.timeout,
            )
            elapsed = time.perf_counter() - start
            stdout = proc.stdout
            stderr = proc.stderr
            returncode = proc.returncode
        except subprocess.TimeoutExpired as exc:
            elapsed = float(config.timeout)
            stdout = (exc.stdout or b"").decode(errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
            stderr = (exc.stderr or b"").decode(errors="replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
            error_msg = f"Timeout after {config.timeout}s"
            return RunResult(
                workload=workload.name,
                config_name=config.name,
                num_threads=config.num_threads,
                cpu_list=list(config.cpu_list),
                metrics={},
                raw_stdout=stdout,
                raw_stderr=stderr,
                returncode=-1,
                wall_time=elapsed,
                iteration=iteration,
                timestamp=datetime.now().isoformat(),
                success=False,
                error_msg=error_msg,
                command=cmd,
            )
        except FileNotFoundError as exc:
            error_msg = f"Command not found: {cmd[0] if cmd else '?'} — {exc}"
            return RunResult(
                workload=workload.name,
                config_name=config.name,
                num_threads=config.num_threads,
                cpu_list=list(config.cpu_list),
                metrics={},
                raw_stdout="",
                raw_stderr="",
                returncode=-1,
                wall_time=0.0,
                iteration=iteration,
                timestamp=datetime.now().isoformat(),
                success=False,
                error_msg=error_msg,
                command=cmd,
            )
        except Exception as exc:
            error_msg = f"Unexpected error running command: {exc}"
            return RunResult(
                workload=workload.name,
                config_name=config.name,
                num_threads=config.num_threads,
                cpu_list=list(config.cpu_list),
                metrics={},
                raw_stdout="",
                raw_stderr="",
                returncode=-1,
                wall_time=elapsed,
                iteration=iteration,
                timestamp=datetime.now().isoformat(),
                success=False,
                error_msg=error_msg,
                command=cmd,
            )

        # Parse metrics
        success = returncode == 0
        metrics: Dict[str, float] = {}
        if success:
            try:
                metrics = workload.parse_output(stdout, stderr, returncode)
                if not metrics:
                    success = False
                    error_msg = "parse_output returned empty metrics"
            except Exception as exc:
                success = False
                error_msg = f"parse_output raised: {exc}"
        else:
            error_msg = f"Process exited with code {returncode}"

        return RunResult(
            workload=workload.name,
            config_name=config.name,
            num_threads=config.num_threads,
            cpu_list=list(config.cpu_list),
            metrics=metrics,
            raw_stdout=stdout,
            raw_stderr=stderr,
            returncode=returncode,
            wall_time=elapsed,
            iteration=iteration,
            timestamp=datetime.now().isoformat(),
            success=success,
            error_msg=error_msg,
            command=cmd,
        )

    def _print_result(self, r: RunResult) -> None:
        status = "OK" if r.success else "FAILED"
        print(
            f"  [{status}] iter={r.iteration} threads={r.num_threads} "
            f"wall={r.wall_time:.2f}s"
        )
        if r.success and r.metrics:
            for k, v in r.metrics.items():
                print(f"    {k}: {v:.4g}")
        elif r.error_msg:
            print(f"    error: {r.error_msg}")
