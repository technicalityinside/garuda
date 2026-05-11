"""
cyclictest — Real-time scheduling latency benchmark.

Measures the latency from when a POSIX timer fires to when the thread is
actually scheduled and wakes up.  Captures interrupt handling overhead,
scheduler jitter, and real-time responsiveness of the kernel.

Run as root (or with CAP_SYS_NICE / CAP_IPC_LOCK) for full RT accuracy.
Without privileges the benchmark still runs but at non-RT priority.

Subsystem: CPU Scheduling / Real-Time

Requirements
------------
  cyclictest  (apt install rt-tests)
"""

import os
import re
import shutil
import sys
from typing import Dict, List, Tuple

_here = os.path.dirname(os.path.abspath(__file__))
_root = os.path.dirname(_here)
if _root not in sys.path:
    sys.path.insert(0, _root)

from benchmark_toolkit.base import BaseWorkload
from benchmark_toolkit.config import BenchmarkConfig
from benchmark_toolkit.sysutils import PackageInstaller


class CyclicTest(BaseWorkload):
    name        = "cyclictest"
    description = "Real-time timer-latency benchmark (cyclictest)"
    version     = "1.0"

    def default_workload_args(self) -> Dict:
        return {
            "loops":       100_000,  # -l: measurement loops per thread
            "interval_us": 1_000,    # -i: timer interval in microseconds
            "priority":    99,       # -p: RT SCHED_FIFO priority (root only)
            "mlockall":    True,     # -m: lock memory pages (root only)
        }

    @property
    def install_hint(self) -> str:
        return "package manager (apt install rt-tests)"

    def validate(self) -> Tuple[bool, str]:
        if not shutil.which("cyclictest"):
            return False, "cyclictest not found in PATH. Install: apt install rt-tests"
        if os.geteuid() != 0:
            return False, (
                "cyclictest requires root (rt-tests >= 2.x always calls "
                "sched_setscheduler regardless of --policy). "
                "Run: sudo python3 main.py ..."
            )
        return True, f"cyclictest found: {shutil.which('cyclictest')}"

    def install(self, install_dir: str, force: bool = False) -> Tuple[bool, str]:
        if not force and shutil.which("cyclictest"):
            return True, f"Already installed: {shutil.which('cyclictest')}"
        return PackageInstaller.ensure_tools(("cyclictest", "rt-tests"))

    def build_command(self, config: BenchmarkConfig) -> List[str]:
        cfg = {**self.default_workload_args(), **config.workload_args}
        cmd = [
            "cyclictest",
            "--quiet",
            f"--loops={int(cfg['loops'])}",
            f"--interval={int(cfg['interval_us'])}",
            f"--threads={config.num_threads}",
        ]
        if cfg.get("priority"):
            cmd.append(f"--priority={int(cfg['priority'])}")
        if cfg.get("mlockall"):
            cmd.append("--mlockall")
        return cmd

    def parse_output(self, stdout: str, stderr: str, returncode: int) -> Dict[str, float]:
        """
        Parse per-thread summary lines printed by --quiet, e.g.:
          T: 0 (1234) P:99 I:1000 C:100000 Min:    4 Act:  5 Avg:  5 Max:   23

        Aggregates across all threads: minimum of mins, average of avgs, maximum of maxs.
        Latency values are in microseconds.
        """
        mins, avgs, maxs = [], [], []

        for line in (stdout + stderr).splitlines():
            m = re.search(
                r'Min:\s*(\d+)\s+Act:\s*\d+\s+Avg:\s*(\d+)\s+Max:\s*(\d+)',
                line,
            )
            if m:
                mins.append(int(m.group(1)))
                avgs.append(int(m.group(2)))
                maxs.append(int(m.group(3)))

        if not mins:
            return {}

        return {
            "latency_min_us": float(min(mins)),
            "latency_avg_us": float(sum(avgs) / len(avgs)),
            "latency_max_us": float(max(maxs)),
        }
