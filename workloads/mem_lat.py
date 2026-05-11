"""
mem_lat — Memory hierarchy latency sweep (lmbench lat_mem_rd).

Uses pointer-chasing with a configurable stride to measure read latency
across the full memory hierarchy: L1/L2/L3 caches through main DRAM.
TLB effects become visible when the working-set size exceeds the L3 cache.

Subsystem: Memory Access, Address Translation (TLB effects at >L3 sizes)

Requirements
------------
  lat_mem_rd  (apt install lmbench)
  On Ubuntu/Debian the binary lands at /usr/lib/lmbench/bin/lat_mem_rd.
"""

import os
import shutil
import sys
from typing import Dict, List, Optional, Tuple

_here = os.path.dirname(os.path.abspath(__file__))
_root = os.path.dirname(_here)
if _root not in sys.path:
    sys.path.insert(0, _root)

from benchmark_toolkit.base import BaseWorkload
from benchmark_toolkit.config import BenchmarkConfig
from benchmark_toolkit.sysutils import PackageInstaller

_LMBENCH_PATHS = [
    "/usr/lib/lmbench/bin/lat_mem_rd",
    "/usr/lib/x86_64-linux-gnu/lmbench/bin/lat_mem_rd",
    "/usr/lib/aarch64-linux-gnu/lmbench/bin/lat_mem_rd",
    # Ubuntu places the binary under an arch-specific sub-directory of bin/
    "/usr/lib/lmbench/bin/x86_64-linux-gnu/lat_mem_rd",
    "/usr/lib/lmbench/bin/aarch64-linux-gnu/lat_mem_rd",
    "/usr/lib/lmbench/bin/riscv64-linux-gnu/lat_mem_rd",
]

# Representative sizes (MB) and their metric label
_TARGET_SIZES_MB: Dict[str, float] = {
    "l1":   0.016,   # 16 KB  — well within typical L1
    "l2":   0.256,   # 256 KB — within typical L2
    "l3":   4.0,     # 4 MB   — within typical L3
    "l3b":  32.0,    # 32 MB  — beyond most L3 caches
    "dram": 256.0,   # 256 MB — main memory
}


def _find_lat_mem_rd() -> Optional[str]:
    p = shutil.which("lat_mem_rd")
    if p:
        return p
    for candidate in _LMBENCH_PATHS:
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    # Last resort: glob for any arch sub-directory Ubuntu may have used
    import glob
    for pat in (
        "/usr/lib/lmbench/bin/*/lat_mem_rd",
        "/usr/lib/*/lmbench/bin/lat_mem_rd",
    ):
        for m in sorted(glob.glob(pat)):
            if os.path.isfile(m) and os.access(m, os.X_OK):
                return m
    return None


def _closest_lat(pairs: List[Tuple[float, float]], target_mb: float) -> Optional[float]:
    if not pairs:
        return None
    return min(pairs, key=lambda p: abs(p[0] - target_mb))[1]


class MemLat(BaseWorkload):
    name        = "mem_lat"
    description = "Memory hierarchy latency sweep (lmbench lat_mem_rd)"
    version     = "1.0"

    _binary: Optional[str] = None

    def default_workload_args(self) -> Dict:
        return {
            "max_size_mb": 512,   # sweep up to this many MB
            "stride":      128,   # stride in bytes (128 = 2 cache lines)
            "trials":      3,     # -t: timing trials per size point
        }

    @property
    def install_hint(self) -> str:
        return "package manager (apt install lmbench)"

    def validate(self) -> Tuple[bool, str]:
        p = _find_lat_mem_rd()
        if p:
            return True, f"lat_mem_rd found: {p}"
        return (
            False,
            "lat_mem_rd not found. Install: apt install lmbench\n"
            "  Binary is typically at /usr/lib/lmbench/bin/lat_mem_rd",
        )

    def install(self, install_dir: str, force: bool = False) -> Tuple[bool, str]:
        if not force and _find_lat_mem_rd():
            return True, f"Already installed: {_find_lat_mem_rd()}"
        # lat_mem_rd is not in PATH after install, so we can't use ensure_tools
        # (which verifies the binary name in PATH). Install the package directly.
        ok, msg = PackageInstaller.install("lmbench")
        if not ok:
            return False, msg
        p = _find_lat_mem_rd()
        if p:
            return True, f"Installed; binary at {p}"
        return False, "lmbench installed but lat_mem_rd not found — check /usr/lib/lmbench/bin/"

    def setup(self, config: BenchmarkConfig, work_dir: str) -> None:
        self._binary = _find_lat_mem_rd()

    def build_command(self, config: BenchmarkConfig) -> List[str]:
        cfg = {**self.default_workload_args(), **config.workload_args}
        binary = self._binary or _find_lat_mem_rd() or "lat_mem_rd"
        return [
            binary,
            "-P", "1",
            "-t", str(int(cfg["trials"])),
            f"{int(cfg['max_size_mb'])}m",
            str(int(cfg["stride"])),
        ]

    def parse_output(self, stdout: str, stderr: str, returncode: int) -> Dict[str, float]:
        """
        Parse SIZE_MB LATENCY_NS pairs from lat_mem_rd output, e.g.:
          "stride=128
          0.000488281 0.879
          ...
          512 80.54
        Report latency at representative L1/L2/L3/DRAM sizes plus overall min/max.
        """
        pairs: List[Tuple[float, float]] = []
        for line in stdout.splitlines():
            line = line.strip()
            if not line or line.startswith('"') or line.startswith('#'):
                continue
            parts = line.split()
            if len(parts) == 2:
                try:
                    pairs.append((float(parts[0]), float(parts[1])))
                except ValueError:
                    pass

        if not pairs:
            return {}

        metrics: Dict[str, float] = {}
        for label, target_mb in _TARGET_SIZES_MB.items():
            lat = _closest_lat(pairs, target_mb)
            if lat is not None:
                metrics[f"lat_{label}_ns"] = lat

        metrics["lat_min_ns"] = min(p[1] for p in pairs)
        metrics["lat_max_ns"] = max(p[1] for p in pairs)
        return metrics
