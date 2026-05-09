"""
sockperf — Network stack latency benchmark (loopback).

Runs a sockperf ping-pong test over the loopback interface, measuring
round-trip socket latency.  Exercises the kernel socket layer, TCP/UDP
send/receive paths, and scheduling of network threads.

Lifecycle
---------
setup()    — start a background sockperf server
run        — sockperf ping-pong client
teardown() — terminate the server process

Subsystem: Networking (socket layer, send/recv path, scheduling)

Requirements
------------
  sockperf  (apt install sockperf)
"""

import os
import re
import shutil
import signal
import subprocess
import sys
import time
from typing import Dict, List, Optional, Tuple

_here = os.path.dirname(os.path.abspath(__file__))
_root = os.path.dirname(_here)
if _root not in sys.path:
    sys.path.insert(0, _root)

from benchmark_toolkit.base import BaseWorkload
from benchmark_toolkit.config import BenchmarkConfig
from benchmark_toolkit.sysutils import PackageInstaller

_PID_FILE = "sockperf_server.pid"


class SockPerf(BaseWorkload):
    name        = "sockperf"
    description = "Network socket latency benchmark via loopback (sockperf)"
    version     = "1.0"

    _server_proc: Optional[subprocess.Popen] = None

    def default_workload_args(self) -> Dict:
        return {
            "port":       11111,  # server port
            "duration":   30,     # test duration in seconds
            "msg_size":   14,     # payload size in bytes (14 = minimum)
            "udp":        False,  # use UDP instead of TCP
        }

    @property
    def install_hint(self) -> str:
        return "package manager (apt install sockperf)"

    def validate(self) -> Tuple[bool, str]:
        if shutil.which("sockperf"):
            return True, f"sockperf found: {shutil.which('sockperf')}"
        return False, "sockperf not found in PATH. Install: apt install sockperf"

    def install(self, install_dir: str, force: bool = False) -> Tuple[bool, str]:
        if not force and shutil.which("sockperf"):
            return True, f"Already installed: {shutil.which('sockperf')}"
        return PackageInstaller.ensure_tools(("sockperf", "sockperf"))

    # ── Lifecycle ──────────────────────────────────────────────────────────────

    def setup(self, config: BenchmarkConfig, work_dir: str) -> None:
        os.makedirs(work_dir, exist_ok=True)
        cfg = {**self.default_workload_args(), **config.workload_args}
        port = int(cfg["port"])
        proto = "--udp" if cfg.get("udp") else "--tcp"

        # Kill any stale server
        pid_file = os.path.join(work_dir, _PID_FILE)
        if os.path.exists(pid_file):
            try:
                with open(pid_file) as f:
                    old_pid = int(f.read().strip())
                os.kill(old_pid, signal.SIGTERM)
            except (ValueError, ProcessLookupError, OSError):
                pass
            os.remove(pid_file)

        print(f"[sockperf] Starting server on port {port} ({proto})...", flush=True)
        self._server_proc = subprocess.Popen(
            ["sockperf", "server", "-i", "127.0.0.1", "-p", str(port), proto],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        with open(pid_file, "w") as f:
            f.write(str(self._server_proc.pid))

        time.sleep(1)
        print("[sockperf] Server ready.", flush=True)

    def teardown(self, config: BenchmarkConfig, work_dir: str) -> None:
        if self._server_proc is not None:
            self._server_proc.terminate()
            try:
                self._server_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._server_proc.kill()
            self._server_proc = None

        pid_file = os.path.join(work_dir, _PID_FILE)
        if os.path.exists(pid_file):
            os.remove(pid_file)
        print("[sockperf] Server stopped.", flush=True)

    # ── Benchmark ──────────────────────────────────────────────────────────────

    def build_command(self, config: BenchmarkConfig) -> List[str]:
        cfg = {**self.default_workload_args(), **config.workload_args}
        proto = "--udp" if cfg.get("udp") else "--tcp"
        return [
            "sockperf", "ping-pong",
            "-i", "127.0.0.1",
            "-p", str(int(cfg["port"])),
            "-t", str(int(cfg["duration"])),
            "--msg-size", str(int(cfg["msg_size"])),
            "--full-rtt",
            proto,
        ]

    def parse_output(self, stdout: str, stderr: str, returncode: int) -> Dict[str, float]:
        """
        Parse sockperf summary lines, e.g.:
          sockperf: Summary: Latency is 4.056 usec
          sockperf: Total 99.000 Percentile is 7.563 usec
          sockperf: Total 99.900 Percentile is 12.456 usec
        """
        metrics: Dict[str, float] = {}
        combined = stdout + "\n" + stderr

        for line in combined.splitlines():
            # Average latency
            m = re.search(r'Summary:\s+Latency is\s+([\d.]+)\s+usec', line)
            if m:
                metrics["latency_avg_us"] = float(m.group(1))
                continue

            # Percentile: "Total 99.000 Percentile is X usec"
            m2 = re.search(r'Total\s+([\d.]+)\s+Percentile is\s+([\d.]+)\s+usec', line)
            if m2:
                pct = float(m2.group(1))
                val = float(m2.group(2))
                if abs(pct - 50.0) < 0.1:
                    metrics["latency_p50_us"] = val
                elif abs(pct - 99.0) < 0.1:
                    metrics["latency_p99_us"] = val
                elif abs(pct - 99.9) < 0.1:
                    metrics["latency_p999_us"] = val

        return metrics
