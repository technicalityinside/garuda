"""
mem_alloc — Kernel memory-allocation throughput benchmark (stress-ng).

Exercises the virtual memory subsystem via malloc/free (glibc → brk/mmap)
or direct anonymous mmap calls.  Stresses the buddy allocator, slab/slub,
and page-fault handler.

Subsystem: Memory Allocation (buddy allocator, slab, brk, mmap, page faults)

Requirements
------------
  stress-ng  (apt install stress-ng)
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

_VALID_STRESSORS = ("malloc", "mmap", "brk")


class MemAlloc(BaseWorkload):
    name        = "mem_alloc"
    description = "Memory allocation throughput benchmark (stress-ng)"
    version     = "1.0"

    # Store the stressor name from the last build_command so parse_output can use it
    _current_stressor: str = "malloc"

    def default_workload_args(self) -> Dict:
        return {
            "stressor": "malloc",  # malloc | mmap | brk
            "duration": 60,        # seconds
        }

    @property
    def install_hint(self) -> str:
        return "package manager (apt install stress-ng)"

    def validate(self) -> Tuple[bool, str]:
        if shutil.which("stress-ng"):
            return True, f"stress-ng found: {shutil.which('stress-ng')}"
        return False, "stress-ng not found in PATH. Install: apt install stress-ng"

    def install(self, install_dir: str, force: bool = False) -> Tuple[bool, str]:
        if not force and shutil.which("stress-ng"):
            return True, f"Already installed: {shutil.which('stress-ng')}"
        return PackageInstaller.ensure_tools(("stress-ng", "stress-ng"))

    def build_command(self, config: BenchmarkConfig) -> List[str]:
        cfg = {**self.default_workload_args(), **config.workload_args}
        stressor = str(cfg["stressor"]).lower()
        if stressor not in _VALID_STRESSORS:
            raise ValueError(
                f"Unknown stressor: {stressor!r}. "
                f"Valid options: {', '.join(_VALID_STRESSORS)}"
            )
        self._current_stressor = stressor
        return [
            "stress-ng",
            f"--{stressor}", str(config.num_threads),
            "--metrics-brief",
            "-t", str(int(cfg["duration"])),
        ]

    def parse_output(self, stdout: str, stderr: str, returncode: int) -> Dict[str, float]:
        """
        Parse stress-ng metrics lines.  Handles both output formats:

        Legacy (< 0.13):
          stress-ng: metrc: [PID] malloc   820587.41 bogo ops/s (real time)

        Table (>= 0.13):
          stress-ng: metrc: [PID] malloc | 24617628 | 30.00s | ... | 820587.41 | ...
        """
        stressor = self._current_stressor
        combined = stdout + "\n" + stderr
        metrics: Dict[str, float] = {}

        for line in combined.splitlines():
            if ("metrc" not in line and "metric" not in line) or stressor not in line:
                continue

            # Legacy format: NUMBER bogo ops/s (real time)
            m = re.search(
                r'([\d.]+(?:e[+-]?\d+)?)\s+bogo ops/s\s+\(real time\)',
                line, re.IGNORECASE,
            )
            if m:
                metrics[f"{stressor}_bogo_ops_per_sec"] = float(m.group(1))
                return metrics

            # Table format: extract the 5th numeric column (ops/s real time)
            if "|" in line:
                parts = [p.strip() for p in line.split("|")]
                nums: List[float] = []
                for p in parts:
                    try:
                        nums.append(float(p.rstrip("s")))
                    except ValueError:
                        pass
                # Columns: bogo_ops | real_s | usr_s | sys_s | ops_s_real | ops_s_usr_sys
                if len(nums) >= 5:
                    metrics[f"{stressor}_bogo_ops_per_sec"] = nums[-2]
                    return metrics

        return metrics
