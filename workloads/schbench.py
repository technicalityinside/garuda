"""
schbench — Linux scheduler wakeup-latency microbenchmark.

Two layers of threads: message-passers wake workers and measure the resulting
latency percentiles.  A direct measure of scheduler responsiveness under load.

Subsystem: CPU Scheduling

Requirements
------------
  schbench  (apt install schbench)
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


class SchBench(BaseWorkload):
    name        = "schbench"
    description = "Scheduler wakeup-latency benchmark (schbench)"
    version     = "1.0"

    def default_workload_args(self) -> Dict:
        return {
            "message_threads": 2,   # -m: message-passer threads
            "worker_threads":  16,  # -t: worker threads per message-passer
            "runtime":         30,  # -r: runtime in seconds
        }

    @property
    def install_hint(self) -> str:
        return "package manager (apt install schbench)"

    def validate(self) -> Tuple[bool, str]:
        if shutil.which("schbench"):
            return True, f"schbench found: {shutil.which('schbench')}"
        return False, "schbench not found in PATH. Install: apt install schbench"

    def install(self, install_dir: str, force: bool = False) -> Tuple[bool, str]:
        if not force and shutil.which("schbench"):
            return True, f"Already installed: {shutil.which('schbench')}"
        return PackageInstaller.ensure_tools(("schbench", "schbench"))

    def build_command(self, config: BenchmarkConfig) -> List[str]:
        cfg = {**self.default_workload_args(), **config.workload_args}
        return [
            "schbench",
            f"-m{int(cfg['message_threads'])}",
            f"-t{int(cfg['worker_threads'])}",
            f"-r{int(cfg['runtime'])}",
        ]

    def parse_output(self, stdout: str, stderr: str, returncode: int) -> Dict[str, float]:
        """
        Parse the Wakeup Latencies percentile block, e.g.:
          Wakeup Latencies percentiles (usec) runtime 30 (s) ...
               50.0th: 4
              *99.0th: 7
             min=0, max=196
        """
        metrics: Dict[str, float] = {}
        in_wakeup = False

        for line in (stdout + stderr).splitlines():
            s = line.strip()
            if "Wakeup Latencies" in s:
                in_wakeup = True
                continue
            if "Worker Latencies" in s:
                break   # stop after the wakeup section

            if not in_wakeup:
                continue

            # "  *99.0th: 7"  or  "   50.0th: 4"
            m = re.match(r'\*?\s*([\d.]+)th:\s*([\d.]+)', s)
            if m:
                pct_raw = m.group(1)   # "50.0", "99.9"
                val = float(m.group(2))
                # "50.0" → "50", "99.9" → "99_9", "99.5" → "99_5"
                pct_key = pct_raw.rstrip("0").rstrip(".").replace(".", "_")
                metrics[f"wakeup_p{pct_key}_us"] = val

            # "min=0, max=196"
            m2 = re.search(r'min=(\d+),\s*max=(\d+)', s)
            if m2:
                metrics["wakeup_min_us"] = float(m2.group(1))
                metrics["wakeup_max_us"] = float(m2.group(2))

        return metrics
