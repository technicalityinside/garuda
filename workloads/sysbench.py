"""
sysbench CPU benchmark workload.

Requires: sysbench (apt install sysbench / yum install sysbench)
"""

import shutil
import sys
import os
from typing import Dict, List, Tuple

_here = os.path.dirname(os.path.abspath(__file__))
_root = os.path.dirname(_here)
if _root not in sys.path:
    sys.path.insert(0, _root)

from benchmark_toolkit.base import BaseWorkload
from benchmark_toolkit.config import BenchmarkConfig


class SysbenchCPU(BaseWorkload):
    name = "sysbench_cpu"
    description = "sysbench CPU benchmark (prime number stress test)"
    version = "1.0"

    def validate(self) -> Tuple[bool, str]:
        if shutil.which("sysbench") is None:
            return False, "sysbench not found in PATH. Install with: apt install sysbench"
        return True, "sysbench found"

    def build_command(self, config: BenchmarkConfig) -> List[str]:
        args = {**self.default_workload_args(), **config.workload_args}
        return [
            "sysbench",
            "cpu",
            f"--threads={config.num_threads}",
            f"--cpu-max-prime={args.get('prime', 20000)}",
            f"--time={args.get('time', 10)}",
            "run",
        ]

    def parse_output(self, stdout: str, stderr: str, returncode: int) -> Dict[str, float]:
        """
        Parse sysbench CPU output.
        Looks for:
          events per second:  1234.56
          total time:         10.0002s
          total number of events:  12345
          min/avg/max/95th latency lines
        """
        metrics: Dict[str, float] = {}
        for line in stdout.splitlines():
            line_stripped = line.strip()
            if "events per second:" in line_stripped:
                try:
                    metrics["events_per_sec"] = float(line_stripped.split(":")[-1].strip())
                except ValueError:
                    pass
            elif "total time:" in line_stripped:
                try:
                    val = line_stripped.split(":")[-1].strip().rstrip("s")
                    metrics["total_time_sec"] = float(val)
                except ValueError:
                    pass
            elif "total number of events:" in line_stripped:
                try:
                    metrics["total_events"] = float(line_stripped.split(":")[-1].strip())
                except ValueError:
                    pass
            elif "min:" in line_stripped and "latency" not in line_stripped.lower():
                # latency min line: "    min:  X.XX"
                try:
                    metrics["latency_min_ms"] = float(line_stripped.split(":")[-1].strip())
                except ValueError:
                    pass
            elif "avg:" in line_stripped:
                try:
                    metrics["latency_avg_ms"] = float(line_stripped.split(":")[-1].strip())
                except ValueError:
                    pass
            elif "max:" in line_stripped:
                try:
                    metrics["latency_max_ms"] = float(line_stripped.split(":")[-1].strip())
                except ValueError:
                    pass
            elif "95th percentile:" in line_stripped:
                try:
                    metrics["latency_p95_ms"] = float(line_stripped.split(":")[-1].strip())
                except ValueError:
                    pass
        return metrics

    def default_workload_args(self) -> Dict:
        return {
            "prime": 20000,
            "time": 10,
        }
