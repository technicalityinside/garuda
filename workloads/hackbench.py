"""
hackbench — Linux scheduler communication-throughput benchmark.

Creates groups of tasks that send and receive messages through sockets or
pipes.  Measures overall scheduling throughput: how fast the kernel can
context-switch and deliver messages between many competing tasks.

Subsystem: CPU Scheduling

Requirements
------------
  hackbench  (apt install rt-tests)
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


class HackBench(BaseWorkload):
    name        = "hackbench"
    description = "Scheduler task-communication throughput benchmark (hackbench)"
    version     = "1.0"

    def default_workload_args(self) -> Dict:
        return {
            "groups":      10,    # -g: number of task groups
            "loops":       1000,  # -l: messages per sender per run
            "data_size":   100,   # -s: message size in bytes
            "use_threads": False, # -T: use threads instead of processes
            "use_pipes":   False, # -p: use pipes instead of sockets
        }

    @property
    def install_hint(self) -> str:
        return "package manager (apt install rt-tests)"

    def validate(self) -> Tuple[bool, str]:
        if shutil.which("hackbench"):
            return True, f"hackbench found: {shutil.which('hackbench')}"
        return False, "hackbench not found in PATH. Install: apt install rt-tests"

    def install(self, install_dir: str, force: bool = False) -> Tuple[bool, str]:
        if not force and shutil.which("hackbench"):
            return True, f"Already installed: {shutil.which('hackbench')}"
        return PackageInstaller.ensure_tools(("hackbench", "rt-tests"))

    def build_command(self, config: BenchmarkConfig) -> List[str]:
        cfg = {**self.default_workload_args(), **config.workload_args}
        cmd = [
            "hackbench",
            "-g", str(int(cfg["groups"])),
            "-l", str(int(cfg["loops"])),
            "-s", str(int(cfg["data_size"])),
        ]
        if cfg.get("use_threads"):
            cmd.append("-T")
        if cfg.get("use_pipes"):
            cmd.append("-p")
        return cmd

    def parse_output(self, stdout: str, stderr: str, returncode: int) -> Dict[str, float]:
        """
        Parse the single result line, e.g.:
          Time: 0.383
        """
        metrics: Dict[str, float] = {}
        for line in (stdout + stderr).splitlines():
            m = re.search(r'Time:\s*([\d.]+)', line)
            if m:
                metrics["time_sec"] = float(m.group(1))
                break
        return metrics
