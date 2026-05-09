"""
iperf3 — Network stack throughput benchmark (loopback).

Runs iperf3 client against a local iperf3 server over the loopback interface.
Loopback bypasses the NIC driver but exercises the full kernel TCP/IP stack:
socket layer, TCP congestion control, sk_buff allocation, and softirq handling.

Lifecycle
---------
setup()    — start a background iperf3 server on the configured port
run        — iperf3 client (JSON output, parsed per iteration)
teardown() — terminate the server process

Subsystem: Networking (TCP/IP stack, socket layer, sk_buff, softirq)

Requirements
------------
  iperf3  (apt install iperf3)
"""

import json
import os
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

_PID_FILE = "iperf3_server.pid"


class Iperf3(BaseWorkload):
    name        = "iperf3"
    description = "Network stack throughput benchmark via loopback (iperf3)"
    version     = "1.0"

    _server_proc: Optional[subprocess.Popen] = None

    def default_workload_args(self) -> Dict:
        return {
            "port":     5201,   # iperf3 port
            "duration": 30,     # -t: test duration in seconds
            "streams":  1,      # -P: parallel streams
            "udp":      False,  # -u: use UDP instead of TCP
            "reverse":  False,  # -R: server → client direction
        }

    @property
    def install_hint(self) -> str:
        return "package manager (apt install iperf3)"

    def validate(self) -> Tuple[bool, str]:
        if shutil.which("iperf3"):
            return True, f"iperf3 found: {shutil.which('iperf3')}"
        return False, "iperf3 not found in PATH. Install: apt install iperf3"

    def install(self, install_dir: str, force: bool = False) -> Tuple[bool, str]:
        if not force and shutil.which("iperf3"):
            return True, f"Already installed: {shutil.which('iperf3')}"
        return PackageInstaller.ensure_tools(("iperf3", "iperf3"))

    # ── Lifecycle ──────────────────────────────────────────────────────────────

    def setup(self, config: BenchmarkConfig, work_dir: str) -> None:
        os.makedirs(work_dir, exist_ok=True)
        cfg = {**self.default_workload_args(), **config.workload_args}
        port = int(cfg["port"])

        # Kill any stale server from a previous run
        pid_file = os.path.join(work_dir, _PID_FILE)
        if os.path.exists(pid_file):
            try:
                with open(pid_file) as f:
                    old_pid = int(f.read().strip())
                os.kill(old_pid, signal.SIGTERM)
            except (ValueError, ProcessLookupError, OSError):
                pass
            os.remove(pid_file)

        print(f"[iperf3] Starting server on port {port}...", flush=True)
        self._server_proc = subprocess.Popen(
            ["iperf3", "-s", "-p", str(port)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        with open(pid_file, "w") as f:
            f.write(str(self._server_proc.pid))

        time.sleep(1)   # let the server socket bind
        print("[iperf3] Server ready.", flush=True)

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
        print("[iperf3] Server stopped.", flush=True)

    # ── Benchmark ──────────────────────────────────────────────────────────────

    def build_command(self, config: BenchmarkConfig) -> List[str]:
        cfg = {**self.default_workload_args(), **config.workload_args}
        cmd = [
            "iperf3",
            "-c", "127.0.0.1",
            "-p", str(int(cfg["port"])),
            "-t", str(int(cfg["duration"])),
            "-P", str(int(cfg["streams"])),
            "--json",
        ]
        if cfg.get("udp"):
            cmd.append("-u")
        if cfg.get("reverse"):
            cmd.append("-R")
        return cmd

    def parse_output(self, stdout: str, stderr: str, returncode: int) -> Dict[str, float]:
        """
        Parse iperf3 JSON output.  Reports throughput in Gbps and TCP retransmits.
        """
        metrics: Dict[str, float] = {}
        json_start = stdout.find("{")
        if json_start == -1:
            return metrics
        try:
            data = json.loads(stdout[json_start:])
        except json.JSONDecodeError:
            return metrics

        end = data.get("end", {})
        recv = end.get("sum_received", end.get("sum", {}))
        sent = end.get("sum_sent",     end.get("sum", {}))

        bps = recv.get("bits_per_second", sent.get("bits_per_second", 0))
        if bps:
            metrics["throughput_gbps"] = bps / 1e9

        retrans = sent.get("retransmits", 0)
        if retrans is not None:
            metrics["retransmits"] = float(retrans)

        return metrics
