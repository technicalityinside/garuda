"""
STREAM memory bandwidth benchmark workload.

Wraps the STREAM binary. Looks for 'stream', 'stream_c', or 'stream_omp'
in PATH and in the work_dir. Falls back gracefully if not found.

OMP_NUM_THREADS controls the thread count for OpenMP-built STREAM binaries.
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

_STREAM_BINARY_NAMES = ["stream_omp", "stream_c", "stream"]


def _find_stream_binary(work_dir: Optional[str] = None) -> Optional[str]:
    """Search for a STREAM binary in PATH and optionally in work_dir."""
    search_dirs: List[str] = []
    if work_dir and os.path.isdir(work_dir):
        search_dirs.append(work_dir)

    for name in _STREAM_BINARY_NAMES:
        # Check PATH first
        found = shutil.which(name)
        if found:
            return found
        # Check work_dir
        for d in search_dirs:
            candidate = os.path.join(d, name)
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                return candidate

    return None


class StreamBench(BaseWorkload):
    name = "stream"
    description = "STREAM memory bandwidth benchmark"
    version = "1.0"

    _binary_path: Optional[str] = None

    def validate(self) -> Tuple[bool, str]:
        path = _find_stream_binary()
        if path:
            return True, f"STREAM binary found: {path}"
        names = ", ".join(_STREAM_BINARY_NAMES)
        return (
            False,
            f"STREAM binary not found. Expected one of: {names} in PATH or work_dir. "
            "Build from https://www.cs.virginia.edu/stream/ or: "
            "apt install stream",
        )

    def setup(self, config: BenchmarkConfig, work_dir: str) -> None:
        """Locate the STREAM binary at setup time."""
        self._binary_path = _find_stream_binary(work_dir)

    def build_command(self, config: BenchmarkConfig) -> List[str]:
        """Return [stream_binary_path]. OMP_NUM_THREADS controls thread count."""
        binary = self._binary_path or _find_stream_binary()
        if not binary:
            # Will fail at runtime; validate() should have caught this
            binary = "stream"
        return [binary]

    def get_env(self, config: BenchmarkConfig) -> Dict[str, str]:
        """STREAM uses OMP_NUM_THREADS."""
        env = super().get_env(config)
        # Some STREAM builds also respect STREAM_ARRAY_SIZE via env
        args = {**self.default_workload_args(), **config.workload_args}
        if "array_size" in args:
            env["STREAM_ARRAY_SIZE"] = str(args["array_size"])
        return env

    def parse_output(self, stdout: str, stderr: str, returncode: int) -> Dict[str, float]:
        """
        Parse the 4 STREAM operation results: Copy, Scale, Add, Triad (MB/s).

        STREAM output lines look like:
          Copy:       12345.6     0.001295     0.001295     0.001295
          Scale:      11234.5     ...
          Add:        12100.3     ...
          Triad:      12200.8     ...
        """
        metrics: Dict[str, float] = {}
        for line in stdout.splitlines():
            for op in ["Copy", "Scale", "Add", "Triad"]:
                if line.strip().startswith(op + ":"):
                    parts = line.split()
                    if len(parts) >= 2:
                        try:
                            metrics[op.lower() + "_mb_s"] = float(parts[1])
                        except ValueError:
                            pass
                    break
        return metrics

    def default_workload_args(self) -> Dict:
        return {}
