"""
fs_mark — Virtual filesystem metadata throughput benchmark.

Creates, syncs, and deletes large numbers of small files, measuring operations
per second.  Stresses the VFS layer: dentry/inode cache, dcache locking,
directory entry management, and the underlying filesystem's metadata path.

Subsystem: Virtual Filesystem (VFS, dentry cache, inode allocation)

Requirements
------------
  fs_mark  (apt install fs-mark  — or built from source automatically)
"""

import os
import re
import shutil
import subprocess
import sys
import tempfile
from typing import Dict, List, Tuple

_here = os.path.dirname(os.path.abspath(__file__))
_root = os.path.dirname(_here)
if _root not in sys.path:
    sys.path.insert(0, _root)

from benchmark_toolkit.base import BaseWorkload
from benchmark_toolkit.config import BenchmarkConfig
from benchmark_toolkit.sysutils import PackageInstaller

_WORK_SUBDIR = "fs_mark_work"


def _build_fs_mark(install_dir: str) -> Tuple[bool, str]:
    """Clone fs_mark from GitHub and compile it."""
    ok, msg = PackageInstaller.ensure_tools(("gcc", "gcc"), ("make", "make"), ("git", "git"))
    if not ok:
        return False, f"Build deps unavailable: {msg}"

    tmpdir = tempfile.mkdtemp(prefix="fs_mark_src_")
    try:
        urls = [
            "https://github.com/josefbacik/fs_mark.git",
            "https://github.com/kernelslacker/fs_mark.git",
        ]
        src_dir = os.path.join(tmpdir, "src")
        cloned = False
        for url in urls:
            r = subprocess.run(
                ["git", "clone", "--depth=1", url, src_dir],
                capture_output=True, text=True,
            )
            if r.returncode == 0:
                cloned = True
                break
        if not cloned:
            return False, "git clone failed for all fs_mark mirror URLs"

        r = subprocess.run(["make"], cwd=src_dir, capture_output=True, text=True)
        if r.returncode != 0:
            return False, f"make failed:\n{r.stderr.strip()}"

        dest = os.path.join(install_dir, "fs_mark")
        shutil.copy2(os.path.join(src_dir, "fs_mark"), dest)
        os.chmod(dest, 0o755)
        return True, f"Built from source and installed: {dest}"
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


class FsMark(BaseWorkload):
    name        = "fs_mark"
    description = "VFS metadata throughput benchmark (fs_mark)"
    version     = "1.0"

    _target_dir: str = "/tmp/fs_mark_work"

    def default_workload_args(self) -> Dict:
        return {
            "num_files":   4096,     # -n: files created per iteration
            "file_size":   4096,     # -s: file size in bytes (0 = metadata-only)
            "iterations":  5,        # -L: number of create-sync-delete cycles
            "dir":         "",       # -d: working directory (default: work_dir subdir)
            "subdirs":     0,        # -D: number of subdirectories (0 = flat)
            "sync_writes": True,     # -S: fsync each file after write
        }

    @property
    def install_hint(self) -> str:
        return "package manager or source build"

    def validate(self) -> Tuple[bool, str]:
        if shutil.which("fs_mark"):
            return True, f"fs_mark found: {shutil.which('fs_mark')}"
        return False, "fs_mark not found in PATH. Run: python3 main.py setup --workload fs_mark"

    def install(self, install_dir: str, force: bool = False) -> Tuple[bool, str]:
        if not force and shutil.which("fs_mark"):
            return True, f"Already installed: {shutil.which('fs_mark')}"

        # Try package manager first (available on some older distros as fs-mark)
        ok, msg = PackageInstaller.ensure_tools(("fs_mark", "fs-mark"))
        if ok:
            return True, msg

        # Package not in repos — build from source
        print("  Package not available in repos — building fs_mark from source...")
        return _build_fs_mark(install_dir)

    # ── Lifecycle ──────────────────────────────────────────────────────────────

    def setup(self, config: BenchmarkConfig, work_dir: str) -> None:
        cfg = {**self.default_workload_args(), **config.workload_args}
        self._target_dir = str(cfg.get("dir") or os.path.join(work_dir, _WORK_SUBDIR))
        os.makedirs(self._target_dir, exist_ok=True)

    def teardown(self, config: BenchmarkConfig, work_dir: str) -> None:
        import shutil as _shutil
        if os.path.isdir(self._target_dir):
            _shutil.rmtree(self._target_dir, ignore_errors=True)

    # ── Benchmark ──────────────────────────────────────────────────────────────

    def build_command(self, config: BenchmarkConfig) -> List[str]:
        cfg = {**self.default_workload_args(), **config.workload_args}
        target_dir = self._target_dir
        cmd = [
            "fs_mark",
            "-d", target_dir,
            "-n", str(int(cfg["num_files"])),
            "-s", str(int(cfg["file_size"])),
            "-t", str(config.num_threads),
            "-L", str(int(cfg["iterations"])),
        ]
        if int(cfg.get("subdirs", 0)) > 0:
            cmd += ["-D", str(int(cfg["subdirs"]))]
        if cfg.get("sync_writes"):
            cmd.append("-S")
        return cmd

    def parse_output(self, stdout: str, stderr: str, returncode: int) -> Dict[str, float]:
        """
        Parse fs_mark output.  The tool prints one result line per iteration
        plus a final average block, e.g.:

          #  File Size  Dir Size  #Threads  #SubDirs  #Files  Time(sec)  Files/sec ...
          1     4096       0         4         0       4096      2.34      1750.4   ...
          ...
          Average File Creation Results:
          Threads  File Size  Files/sec   App Overhead
              4       4096    1812.3         0.00

        Report the average files/sec from the summary block.
        """
        metrics: Dict[str, float] = {}
        in_avg_block = False
        per_iter_values: List[float] = []

        for line in stdout.splitlines():
            s = line.strip()

            if "Average File Creation" in s:
                in_avg_block = True
                continue

            if in_avg_block:
                # "    4       4096    1812.3    0.00"
                parts = s.split()
                if len(parts) >= 3:
                    try:
                        metrics["files_per_sec"] = float(parts[2])
                    except ValueError:
                        pass
                in_avg_block = False
                continue

            # Also parse per-iteration lines: "1  4096  0  4  0  4096  2.34  1750.4  ..."
            # Column 8 (0-indexed 7) is Files/sec — skip header lines
            if s and s[0].isdigit():
                parts = s.split()
                if len(parts) >= 8:
                    try:
                        per_iter_values.append(float(parts[7]))
                    except ValueError:
                        pass

        # Fall back to mean of per-iteration values if average block was not found
        if "files_per_sec" not in metrics and per_iter_values:
            metrics["files_per_sec"] = sum(per_iter_values) / len(per_iter_values)

        return metrics
