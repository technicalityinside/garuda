"""
Pure-Python CPU benchmark using multiprocessing.

No external dependencies required. Always works.

The benchmark runs a Sieve of Eratosthenes up to 1,000,000 repeatedly
across N worker processes for a fixed duration and reports total ops/sec.
"""

import os
import sys
import textwrap
from typing import Dict, List, Tuple

# Import path gymnastics: workloads/ is discovered dynamically, so we need
# to find benchmark_toolkit relative to this file's location.
_here = os.path.dirname(os.path.abspath(__file__))
_root = os.path.dirname(_here)
if _root not in sys.path:
    sys.path.insert(0, _root)

from benchmark_toolkit.base import BaseWorkload
from benchmark_toolkit.config import BenchmarkConfig

# Worker script written inline as a string so there is no separate file
_WORKER_SCRIPT = textwrap.dedent("""\
    #!/usr/bin/env python3
    \"\"\"
    python_bench worker: Sieve of Eratosthenes multiprocessing CPU benchmark.
    Usage: python python_bench_worker.py --workers N --duration 10
    \"\"\"
    import argparse
    import multiprocessing
    import time


    def sieve(limit: int) -> int:
        \"\"\"Sieve of Eratosthenes up to limit. Returns count of primes found.\"\"\"
        is_prime = bytearray([1]) * (limit + 1)
        is_prime[0] = 0
        if limit >= 1:
            is_prime[1] = 0
        for i in range(2, int(limit ** 0.5) + 1):
            if is_prime[i]:
                is_prime[i * i :: i] = bytearray(len(is_prime[i * i :: i]))
        return sum(is_prime)


    def worker_main(args_tuple):
        \"\"\"Worker function: runs sieve repeatedly for `duration` seconds.\"\"\"
        duration, limit = args_tuple
        count = 0
        end_time = time.monotonic() + duration
        while time.monotonic() < end_time:
            sieve(limit)
            count += 1
        return count


    def main():
        parser = argparse.ArgumentParser(description="python_bench worker")
        parser.add_argument("--workers", type=int, default=1,
                            help="Number of worker processes")
        parser.add_argument("--duration", type=float, default=10.0,
                            help="Duration in seconds per worker")
        parser.add_argument("--limit", type=int, default=1_000_000,
                            help="Sieve upper bound")
        args = parser.parse_args()

        # Use 'spawn' or 'fork' context. 'fork' is faster on Linux.
        try:
            ctx = multiprocessing.get_context("fork")
        except ValueError:
            ctx = multiprocessing.get_context("spawn")

        with ctx.Pool(processes=args.workers) as pool:
            task_args = [(args.duration, args.limit)] * args.workers
            counts = pool.map(worker_main, task_args)

        total_ops = sum(counts)
        total_time = args.duration  # wall-time for one worker = duration
        ops_per_sec = total_ops / total_time

        print(f"ops_per_sec: {ops_per_sec:.4f}")
        print(f"total_ops: {total_ops}")
        print(f"workers: {args.workers}")
        print(f"duration_sec: {args.duration:.2f}")


    if __name__ == "__main__":
        main()
""")


class PythonBench(BaseWorkload):
    name = "python_bench"
    description = "Pure Python multiprocessing CPU benchmark (prime sieve)"
    version = "1.0"

    # Path to the written worker script (set during setup)
    _worker_script_path: str = ""

    def validate(self) -> Tuple[bool, str]:
        return True, "No external dependencies required"

    @property
    def install_hint(self) -> str:
        return "no install needed"

    def setup(self, config: BenchmarkConfig, work_dir: str) -> None:
        """Write the worker script to work_dir."""
        os.makedirs(work_dir, exist_ok=True)
        script_path = os.path.join(work_dir, "python_bench_worker.py")
        with open(script_path, "w") as f:
            f.write(_WORKER_SCRIPT)
        os.chmod(script_path, 0o755)
        self._worker_script_path = script_path

    def build_command(self, config: BenchmarkConfig) -> List[str]:
        """Return command to run the worker script."""
        script_path = self._worker_script_path
        if not script_path:
            # Fallback: try default location
            script_path = os.path.join("/tmp/benchmark_toolkit", "python_bench_worker.py")

        args = {**self.default_workload_args(), **config.workload_args}
        duration = args.get("duration", 10)
        limit = args.get("limit", 1_000_000)

        return [
            sys.executable,
            script_path,
            "--workers", str(config.num_threads),
            "--duration", str(duration),
            "--limit", str(limit),
        ]

    def get_env(self, config: BenchmarkConfig) -> Dict[str, str]:
        """Override: don't set OMP_NUM_THREADS — we control workers via --workers arg."""
        return {
            **config.env_vars,
        }

    def parse_output(self, stdout: str, stderr: str, returncode: int) -> Dict[str, float]:
        """Parse 'ops_per_sec: 123.45' from stdout."""
        metrics: Dict[str, float] = {}
        for line in stdout.splitlines():
            line = line.strip()
            if line.startswith("ops_per_sec:"):
                try:
                    metrics["ops_per_sec"] = float(line.split(":", 1)[1].strip())
                except ValueError:
                    pass
            elif line.startswith("total_ops:"):
                try:
                    metrics["total_ops"] = float(line.split(":", 1)[1].strip())
                except ValueError:
                    pass
            elif line.startswith("duration_sec:"):
                try:
                    metrics["duration_sec"] = float(line.split(":", 1)[1].strip())
                except ValueError:
                    pass
        return metrics

    def default_workload_args(self) -> Dict:
        return {
            "duration": 10,
            "limit": 1_000_000,
        }

    def teardown(self, config: BenchmarkConfig, work_dir: str) -> None:
        """Leave the worker script in place (it may be reused)."""
        pass
