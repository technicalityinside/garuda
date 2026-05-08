"""
FIO flexible I/O benchmark workload.

Requires: fio (apt install fio / yum install fio)
Outputs JSON, parsed for read/write IOPS and bandwidth.
"""

import json
import os
import shutil
import sys
from typing import Dict, List, Tuple

_here = os.path.dirname(os.path.abspath(__file__))
_root = os.path.dirname(_here)
if _root not in sys.path:
    sys.path.insert(0, _root)

from benchmark_toolkit.base import BaseWorkload
from benchmark_toolkit.config import BenchmarkConfig


class FioBench(BaseWorkload):
    name = "fio"
    description = "FIO flexible I/O benchmark"
    version = "1.0"

    def validate(self) -> Tuple[bool, str]:
        if shutil.which("fio") is None:
            return False, "fio not found in PATH. Install with: apt install fio"
        return True, "fio found"

    def build_command(self, config: BenchmarkConfig) -> List[str]:
        args = {**self.default_workload_args(), **config.workload_args}
        directory = args.get("directory", "/tmp")
        return [
            "fio",
            f"--name={args.get('name', 'benchmark')}",
            f"--rw={args.get('rw', 'randread')}",
            f"--bs={args.get('bs', '4k')}",
            f"--size={args.get('size', '1G')}",
            f"--numjobs={config.num_threads}",
            f"--runtime={args.get('runtime', '30')}",
            "--time_based",
            "--output-format=json",
            f"--directory={directory}",
            "--unlink=1",
        ]

    def parse_output(self, stdout: str, stderr: str, returncode: int) -> Dict[str, float]:
        """
        Parse fio JSON output.

        Aggregates read/write IOPS and bandwidth across all jobs.
        Only includes metrics where values are > 0.
        """
        metrics: Dict[str, float] = {}

        # fio sometimes emits non-JSON lines before the JSON blob;
        # find the start of the JSON object.
        json_start = stdout.find("{")
        if json_start == -1:
            return metrics

        try:
            data = json.loads(stdout[json_start:])
        except json.JSONDecodeError:
            return metrics

        total_read_iops = 0.0
        total_read_bw = 0.0
        total_write_iops = 0.0
        total_write_bw = 0.0

        for job in data.get("jobs", []):
            read_section = job.get("read", {})
            write_section = job.get("write", {})

            r_iops = float(read_section.get("iops", 0))
            r_bw = float(read_section.get("bw", 0))
            w_iops = float(write_section.get("iops", 0))
            w_bw = float(write_section.get("bw", 0))

            total_read_iops += r_iops
            total_read_bw += r_bw
            total_write_iops += w_iops
            total_write_bw += w_bw

        if total_read_iops > 0:
            metrics["read_iops"] = total_read_iops
            metrics["read_bw_kb_s"] = total_read_bw
        if total_write_iops > 0:
            metrics["write_iops"] = total_write_iops
            metrics["write_bw_kb_s"] = total_write_bw

        # Also capture latency if available (from last job)
        if data.get("jobs"):
            last_job = data["jobs"][-1]
            for rw in ["read", "write"]:
                lat = last_job.get(rw, {}).get("lat_ns", {})
                if lat and lat.get("mean", 0) > 0:
                    metrics[f"{rw}_lat_us"] = lat["mean"] / 1000.0

        return metrics

    def install(self, install_dir: str, force: bool = False) -> Tuple[bool, str]:
        import subprocess
        if not force and shutil.which("fio"):
            return True, f"Already installed: {shutil.which('fio')}"
        for pm, cmd in [
            ("apt-get", ["sudo", "apt-get", "install", "-y", "fio"]),
            ("apt",     ["sudo", "apt",     "install", "-y", "fio"]),
            ("yum",     ["sudo", "yum",     "install", "-y", "fio"]),
            ("dnf",     ["sudo", "dnf",     "install", "-y", "fio"]),
            ("pacman",  ["sudo", "pacman",  "-S", "--noconfirm", "fio"]),
            ("zypper",  ["sudo", "zypper",  "install", "-y", "fio"]),
        ]:
            if shutil.which(pm):
                print(f"  Installing via {pm} ...")
                result = subprocess.run(cmd)
                if result.returncode == 0 and shutil.which("fio"):
                    return True, f"Installed via {pm}: {shutil.which('fio')}"
                return False, f"{pm} install failed (exit {result.returncode})"
        return (
            False,
            "No supported package manager found. Install manually:\n"
            "  Ubuntu/Debian: sudo apt install fio\n"
            "  RHEL/CentOS:   sudo yum install fio\n"
            "  Source:        https://github.com/axboe/fio",
        )

    @property
    def install_hint(self) -> str:
        return "package manager (apt/yum/dnf)"

    def default_workload_args(self) -> Dict:
        return {
            "name": "benchmark",
            "rw": "randread",
            "bs": "4k",
            "size": "1G",
            "runtime": "30",
            "directory": "/tmp",
        }
