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


def _detect_stream_array_size() -> int:
    """
    Return STREAM_ARRAY_SIZE so the 3 arrays total ≥ 4× the last-level cache.
    Each element is a double (8 bytes); total memory = array_size × 3 × 8 bytes.
    Falls back to 80,000,000 (~1.9 GB total) if cache size detection fails.
    """
    import subprocess
    try:
        out = subprocess.run(["lscpu"], capture_output=True, text=True, timeout=5).stdout
        for line in out.splitlines():
            line = line.strip()
            if line.startswith("L3 cache:"):
                # e.g. "L3 cache:   64 MiB (2 instances)"
                parts = line.split(":", 1)[1].strip().split()
                val = float(parts[0])
                unit = parts[1].upper() if len(parts) > 1 else "MIB"
                l3_mb = val * 1024 if ("GIB" in unit or "GB" in unit) else val
                # 4× L3 spread across 3 arrays of doubles
                array_size = int(4 * l3_mb * 1024 * 1024 / 8 / 3) + 1
                return max(array_size, 10_000_000)
    except Exception:
        pass
    return 80_000_000


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

    def install(self, install_dir: str, force: bool = False) -> Tuple[bool, str]:
        import subprocess
        import tempfile
        import urllib.request
        from benchmark_toolkit.sysutils import PackageInstaller

        target = os.path.join(install_dir, "stream_omp")
        if not force and _find_stream_binary(install_dir):
            return True, f"Already installed: {_find_stream_binary(install_dir)}"

        ok, msg = PackageInstaller.ensure_tools(("gcc", "gcc"))
        if not ok:
            return False, f"gcc unavailable: {msg}"

        array_size = _detect_stream_array_size()
        url = "https://www.cs.virginia.edu/stream/FTP/Code/stream.c"

        print(f"  Downloading stream.c from {url} ...")
        with tempfile.TemporaryDirectory() as tmpdir:
            src = os.path.join(tmpdir, "stream.c")
            try:
                urllib.request.urlretrieve(url, src)
            except Exception as exc:
                return False, f"Download failed: {exc}"

            os.makedirs(install_dir, exist_ok=True)
            print(f"  Compiling with OpenMP (STREAM_ARRAY_SIZE={array_size:,}) ...")
            result = subprocess.run(
                ["gcc", "-O3", "-march=native", "-fopenmp",
                 f"-DSTREAM_ARRAY_SIZE={array_size}", "-o", target, src],
                capture_output=True, text=True,
            )
            if result.returncode == 0:
                return True, f"Compiled to {target}  (array_size={array_size:,})"

            # Retry without OpenMP for single-threaded fallback
            print("  OpenMP failed, retrying without -fopenmp ...")
            target_c = os.path.join(install_dir, "stream_c")
            result2 = subprocess.run(
                ["gcc", "-O3", "-march=native",
                 f"-DSTREAM_ARRAY_SIZE={array_size}", "-o", target_c, src],
                capture_output=True, text=True,
            )
            if result2.returncode == 0:
                return (
                    True,
                    f"Compiled (no OpenMP — single-threaded only) to {target_c}",
                )
            return False, f"Compilation failed:\n{result.stderr}\n{result2.stderr}"

    @property
    def install_hint(self) -> str:
        return "compile from source (gcc + OpenMP)"

    def default_workload_args(self) -> Dict:
        return {}
