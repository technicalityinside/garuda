"""
Multichase memory latency benchmark.

Requires: multichase binary in PATH
Build from: https://github.com/google/multichase
  git clone https://github.com/google/multichase
  cd multichase && make && cp multichase /usr/local/bin/

Runs multichase across a configurable list of arena sizes to produce a
latency-vs-memory-level profile. Each arena size is measured independently.

Key flags used:
  -m <size>  arena size (k/m/g suffix)
  -t <N>     number of parallel chasing threads
  -n <N>     samples (each sample = 0.5 s; default 6 → 3 s per arena)
  -s <size>  stride between pointers (default 256 bytes)
  -X         disable multichase's internal thread affinity (we use taskset)
  -a         report average latency instead of best latency
  -H         use transparent hugepages
  -c <mode>  chase mode: simple, parallel2..parallel10, work:N, incr, branch
"""

import os
import sys
import textwrap
from typing import Dict, List, Tuple

_here = os.path.dirname(os.path.abspath(__file__))
_root = os.path.dirname(_here)
if _root not in sys.path:
    sys.path.insert(0, _root)

from benchmark_toolkit.base import BaseWorkload
from benchmark_toolkit.config import BenchmarkConfig

# Wrapper script: runs multichase once per arena size, prints structured output.
_SWEEP_SCRIPT = textwrap.dedent("""\
    #!/usr/bin/env python3
    import argparse
    import subprocess
    import sys

    def main():
        parser = argparse.ArgumentParser()
        parser.add_argument("--arenas",   required=True,
                            help="Comma-separated arena sizes, e.g. 64k,1m,64m,256m")
        parser.add_argument("--threads",  type=int,   default=1)
        parser.add_argument("--samples",  type=int,   default=6)
        parser.add_argument("--stride",   default="256")
        parser.add_argument("--mode",     default="simple")
        parser.add_argument("--average",  action="store_true")
        parser.add_argument("--hugepages",action="store_true")
        args = parser.parse_args()

        arenas = [a.strip() for a in args.arenas.split(",") if a.strip()]

        for arena in arenas:
            cmd = [
                "multichase",
                "-X",                        # let taskset handle affinity
                "-m", arena,
                "-s", args.stride,
                "-t", str(args.threads),
                "-n", str(args.samples),
            ]
            if args.mode != "simple":
                cmd += ["-c", args.mode]
            if args.average:
                cmd += ["-a"]
            if args.hugepages:
                cmd += ["-H"]

            try:
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
                latency = float(result.stdout.strip())
                print(f"size={arena} latency_ns={latency:.3f}")
            except (ValueError, subprocess.TimeoutExpired) as e:
                print(f"size={arena} latency_ns=ERROR reason={e}", file=sys.stderr)
                sys.exit(1)

    if __name__ == "__main__":
        main()
""")

# Default arena sizes chosen to cover the cache hierarchy of a typical x86 system:
#   64k  → L1 cache (most CPUs have 32–64 KB L1d per core)
#   512k → L2 cache (512 KB – 1 MB L2 per core)
#   4m   → between L2 and L3
#   32m  → L3 cache (32–64 MB shared L3)
#   128m → above L3 → DRAM
#   256m → solidly in DRAM
_DEFAULT_ARENAS = "64k,512k,4m,32m,128m,256m"


class MultichaseBench(BaseWorkload):
    name = "multichase"
    description = "Memory latency benchmark via pointer chasing (latency vs arena size)"
    version = "1.0"

    _sweep_script_path: str = ""

    def validate(self) -> Tuple[bool, str]:
        import shutil
        if shutil.which("multichase") is None:
            return (
                False,
                "multichase not found in PATH. "
                "Build from https://github.com/google/multichase: "
                "git clone https://github.com/google/multichase && "
                "cd multichase && make && cp multichase /usr/local/bin/",
            )
        return True, "multichase found"

    def setup(self, config: BenchmarkConfig, work_dir: str) -> None:
        os.makedirs(work_dir, exist_ok=True)
        path = os.path.join(work_dir, "multichase_sweep.py")
        with open(path, "w") as f:
            f.write(_SWEEP_SCRIPT)
        os.chmod(path, 0o755)
        self._sweep_script_path = path

    def build_command(self, config: BenchmarkConfig) -> List[str]:
        script = self._sweep_script_path
        if not script:
            script = os.path.join("/tmp/benchmark_toolkit", "multichase_sweep.py")

        args = {**self.default_workload_args(), **config.workload_args}

        cmd = [
            sys.executable, script,
            "--arenas",  str(args["arena"]),
            "--threads", str(config.num_threads),
            "--samples", str(args["samples"]),
            "--stride",  str(args["stride"]),
            "--mode",    str(args["mode"]),
        ]
        if args.get("average"):
            cmd += ["--average"]
        if args.get("hugepages"):
            cmd += ["--hugepages"]
        return cmd

    def get_env(self, config: BenchmarkConfig) -> Dict[str, str]:
        # multichase does not use OMP_NUM_THREADS; thread count comes from -t
        return dict(config.env_vars)

    def parse_output(self, stdout: str, stderr: str, returncode: int) -> Dict[str, float]:
        """
        Parse sweep output lines:
          size=64k    latency_ns=2.341
          size=256m   latency_ns=79.123

        Produces per-arena metrics like latency_64k_ns, latency_256m_ns,
        plus latency_min_ns (cache hit) and latency_max_ns (DRAM).
        """
        metrics: Dict[str, float] = {}

        for line in stdout.splitlines():
            line = line.strip()
            if not line or "latency_ns=ERROR" in line:
                continue
            try:
                parts = dict(item.split("=", 1) for item in line.split())
                size = parts["size"].lower()
                latency = float(parts["latency_ns"])
                metrics[f"latency_{size}_ns"] = latency
            except (KeyError, ValueError):
                continue

        if metrics:
            vals = list(metrics.values())
            metrics["latency_min_ns"] = min(vals)   # smallest arena ≈ L1 hit latency
            metrics["latency_max_ns"] = max(vals)   # largest arena ≈ DRAM latency

        return metrics

    def install(self, install_dir: str, force: bool = False) -> Tuple[bool, str]:
        import shutil as _shutil
        import subprocess
        import tempfile
        import urllib.request
        from benchmark_toolkit.sysutils import PackageInstaller

        target = os.path.join(install_dir, "multichase")
        if not force and (_shutil.which("multichase") or
                          (os.path.isfile(target) and os.access(target, os.X_OK))):
            existing = target if os.path.isfile(target) else _shutil.which("multichase")
            return True, f"Already installed: {existing}"

        os.makedirs(install_dir, exist_ok=True)

        # Ensure compiler and build tools are present
        ok, msg = PackageInstaller.ensure_tools(
            ("gcc",  "gcc"),
            ("make", "make"),
        )
        if not ok:
            return False, f"Build tools unavailable: {msg}"

        # Prefer git; fall back to downloading a tarball if git is absent
        with tempfile.TemporaryDirectory() as tmpdir:
            src_dir = os.path.join(tmpdir, "multichase")

            if _shutil.which("git"):
                url = "https://github.com/google/multichase"
                print(f"  Cloning {url} ...")
                r = subprocess.run(
                    ["git", "clone", "--depth=1", url, src_dir],
                    capture_output=True, text=True,
                )
                if r.returncode != 0:
                    return False, f"git clone failed:\n{r.stderr}"
            else:
                # Try to install git first
                print("  git not found — attempting to install ...")
                PackageInstaller.install("git")

                if _shutil.which("git"):
                    url = "https://github.com/google/multichase"
                    print(f"  Cloning {url} ...")
                    r = subprocess.run(
                        ["git", "clone", "--depth=1", url, src_dir],
                        capture_output=True, text=True,
                    )
                    if r.returncode != 0:
                        return False, f"git clone failed:\n{r.stderr}"
                else:
                    # Last resort: download archive
                    archive_url = (
                        "https://github.com/google/multichase/archive"
                        "/refs/heads/master.tar.gz"
                    )
                    print(f"  Falling back to archive download from {archive_url} ...")
                    archive = os.path.join(tmpdir, "multichase.tar.gz")
                    try:
                        urllib.request.urlretrieve(archive_url, archive)
                    except Exception as exc:
                        return False, f"Download failed: {exc}"
                    subprocess.run(["tar", "xzf", archive, "-C", tmpdir], check=True)
                    extracted = os.path.join(tmpdir, "multichase-master")
                    if not os.path.isdir(extracted):
                        return False, "Could not extract archive"
                    os.rename(extracted, src_dir)

            nproc = os.cpu_count() or 4
            print(f"  Building (make -j{nproc}) ...")
            r = subprocess.run(
                ["make", "-j", str(nproc)],
                capture_output=True, text=True,
                cwd=src_dir,
            )
            if r.returncode != 0:
                return False, f"Build failed:\n{r.stderr}"

            built = os.path.join(src_dir, "multichase")
            if not os.path.isfile(built):
                return False, "Build succeeded but multichase binary not found"

            import shutil as _sh
            _sh.copy2(built, target)
            os.chmod(target, 0o755)

        return True, f"Installed to {target}"

    @property
    def install_hint(self) -> str:
        return "git clone + make  (https://github.com/google/multichase)"

    def default_workload_args(self) -> Dict:
        return {
            "arena":     _DEFAULT_ARENAS,  # comma-separated list of sizes
            "samples":   6,                # 6 × 0.5 s = 3 s per arena size
            "stride":    "256",            # 256-byte stride (default pointer spacing)
            "mode":      "simple",         # chase mode: simple, parallel2..10, work:N
            "average":   False,            # False → report best latency; True → average
            "hugepages": False,
        }
