"""
sysbench OLTP benchmark against a MySQL 8 Docker container.

Lifecycle
---------
setup()    — pull MySQL image, start container, wait for ready, run sysbench prepare
run        — sysbench <test> run  (CPU-pinned by the benchmark runner via taskset)
teardown() — sysbench cleanup, docker stop + rm

CPU pinning
-----------
Two independent layers of pinning are available:

  mysql_cpus / mysql_mems  (--arg mysql_cpus=0-3 mysql_mems=0)
      Pin the MySQL Docker container to specific CPU cores and/or NUMA memory
      nodes via Docker's --cpuset-cpus / --cpuset-mems flags.  Accepts any
      value that Docker accepts: "0", "0,1,2,3", "0-3", "0-3,8-11", etc.

  BenchmarkConfig.cpu_list  (--config / --threads)
      Pin the sysbench client process to cores via taskset, as with every
      other workload in the toolkit.

Combining both lets you isolate server and client onto disjoint core sets,
which produces cleaner per-core measurements.

Requirements
------------
  - sysbench  (apt install sysbench)
  - docker    (https://docs.docker.com/engine/install/)
  - Docker daemon must be running and accessible by the current user
"""

import os
import re
import shutil
import subprocess
import sys
import time
from typing import Dict, List, Tuple

_here = os.path.dirname(os.path.abspath(__file__))
_root = os.path.dirname(_here)
if _root not in sys.path:
    sys.path.insert(0, _root)

from benchmark_toolkit.base import BaseWorkload
from benchmark_toolkit.config import BenchmarkConfig

# ── Constants ─────────────────────────────────────────────────────────────────

_MYSQL_USER     = "root"
_MYSQL_DB       = "sysbench"
_MYSQL_PASSWORD = "SysBench#2025"   # container-local; not exposed to internet

_CONTAINER_ID_FILE = "sysbench_mysql_container.txt"

# Valid sysbench OLTP test names (sysbench >= 1.0)
_VALID_TESTS = (
    "oltp_read_write",
    "oltp_read_only",
    "oltp_write_only",
    "oltp_point_select",
    "oltp_update_index",
    "oltp_update_non_index",
    "oltp_insert",
    "oltp_delete",
    "bulk_insert",
    "select_random_points",
    "select_random_ranges",
)


# ── Workload ──────────────────────────────────────────────────────────────────

class SysbenchMySQL(BaseWorkload):
    name        = "sysbench_mysql"
    description = "sysbench OLTP benchmark against a MySQL 8 Docker container"
    version     = "1.0"

    # ── Configuration ──────────────────────────────────────────────────────────

    def default_workload_args(self) -> Dict:
        return {
            "test":            "oltp_read_write",  # sysbench OLTP test name
            "tables":          10,                 # number of test tables
            "table_size":      100_000,            # rows per table
            "time":            60,                 # benchmark duration (seconds)
            "report_interval": 10,                 # per-interval stdout report (seconds)
            "mysql_port":      13306,              # host port mapped to container 3306
            "mysql_image":     "mysql:8.0",        # Docker image to pull
            # CPU/NUMA pinning for the MySQL container (passed to docker --cpuset-*)
            "mysql_cpus":      "",                 # e.g. "0-3" or "0,1,2,3"; "" = no pin
            "mysql_mems":      "",                 # e.g. "0" or "0,1";        "" = no pin
        }

    @property
    def install_hint(self) -> str:
        return "package manager (sysbench) + Docker Engine"

    # ── Validation & install ───────────────────────────────────────────────────

    def validate(self) -> Tuple[bool, str]:
        missing = [t for t in ("sysbench", "docker") if not shutil.which(t)]
        if missing:
            return False, f"Missing: {', '.join(missing)}"
        r = subprocess.run(["docker", "info"], capture_output=True)
        if r.returncode != 0:
            return False, "Docker daemon is not running (run: sudo systemctl start docker)"
        return True, "sysbench and Docker OK"

    def install(self, install_dir: str, force: bool = False) -> Tuple[bool, str]:
        # Install sysbench via the system package manager
        if not force and shutil.which("sysbench"):
            sb_ok = True
        else:
            sb_ok = False
            for pm, cmd in [
                ("apt-get", ["sudo", "apt-get", "install", "-y", "sysbench"]),
                ("apt",     ["sudo", "apt",     "install", "-y", "sysbench"]),
                ("yum",     ["sudo", "yum",     "install", "-y", "sysbench"]),
                ("dnf",     ["sudo", "dnf",     "install", "-y", "sysbench"]),
                ("pacman",  ["sudo", "pacman",  "-S", "--noconfirm", "sysbench"]),
                ("zypper",  ["sudo", "zypper",  "install", "-y", "sysbench"]),
            ]:
                if shutil.which(pm):
                    print(f"  Installing sysbench via {pm}...")
                    res = subprocess.run(cmd)
                    if res.returncode == 0 and shutil.which("sysbench"):
                        sb_ok = True
                        break
                    return False, f"{pm} install failed (exit {res.returncode})"

        if not sb_ok:
            return False, (
                "No supported package manager found. Install sysbench manually:\n"
                "  Ubuntu/Debian: sudo apt install sysbench\n"
                "  RHEL/CentOS:   sudo yum install sysbench"
            )

        if not shutil.which("docker"):
            return False, (
                "sysbench installed but Docker Engine is not present.\n"
                "Install it from https://docs.docker.com/engine/install/"
            )

        # Verify daemon is reachable; try to start it if not
        r = subprocess.run(["docker", "info"], capture_output=True)
        if r.returncode != 0:
            print("  Docker daemon not running — attempting to start it...")
            daemon_up = False
            for start_cmd in (
                ["sudo", "systemctl", "start", "docker"],
                ["sudo", "snap",      "start", "docker"],
                ["sudo", "service",   "docker", "start"],
            ):
                if not shutil.which(start_cmd[1]):
                    continue
                subprocess.run(start_cmd, capture_output=True)
                time.sleep(3)
                if subprocess.run(["docker", "info"], capture_output=True).returncode == 0:
                    daemon_up = True
                    break
            if not daemon_up:
                return False, (
                    "Docker daemon is not running and could not be started automatically.\n"
                    "Run:  sudo systemctl start docker\n"
                    "  or: sudo snap start docker"
                )

        return True, "sysbench and Docker daemon ready"

    # ── Internal helpers ───────────────────────────────────────────────────────

    def _cfg(self, config: BenchmarkConfig) -> Dict:
        return {**self.default_workload_args(), **config.workload_args}

    def _read_container(self, work_dir: str) -> str:
        path = os.path.join(work_dir, _CONTAINER_ID_FILE)
        if os.path.exists(path):
            with open(path) as f:
                return f.read().strip()
        return ""

    def _write_container(self, work_dir: str, name: str) -> None:
        with open(os.path.join(work_dir, _CONTAINER_ID_FILE), "w") as f:
            f.write(name)

    def _remove_container_file(self, work_dir: str) -> None:
        path = os.path.join(work_dir, _CONTAINER_ID_FILE)
        if os.path.exists(path):
            os.remove(path)

    def _sysbench_cmd(self, cfg: Dict, num_threads: int, action: str) -> List[str]:
        test = cfg["test"]
        if test not in _VALID_TESTS:
            raise ValueError(
                f"Unknown sysbench test: {test!r}. Valid options: {', '.join(_VALID_TESTS)}"
            )
        cmd = [
            "sysbench", test,
            f"--mysql-host=127.0.0.1",
            f"--mysql-port={int(cfg['mysql_port'])}",
            f"--mysql-user={_MYSQL_USER}",
            f"--mysql-password={_MYSQL_PASSWORD}",
            f"--mysql-db={_MYSQL_DB}",
            f"--tables={int(cfg['tables'])}",
            f"--table-size={int(cfg['table_size'])}",
            f"--threads={num_threads}",
        ]
        if action == "run":
            cmd += [
                f"--time={int(cfg['time'])}",
                f"--report-interval={int(cfg['report_interval'])}",
            ]
        cmd.append(action)
        return cmd

    def _wait_for_mysql(self, container: str, port: int, timeout: int = 120) -> None:
        print(f"[sysbench_mysql] Waiting for MySQL on port {port}...", flush=True)
        deadline = time.time() + timeout
        while time.time() < deadline:
            r = subprocess.run(
                [
                    "docker", "exec", container,
                    "mysqladmin", "ping",
                    "-h", "127.0.0.1",
                    "-u", _MYSQL_USER,
                    f"-p{_MYSQL_PASSWORD}",
                    "--silent",
                ],
                capture_output=True,
            )
            if r.returncode == 0:
                time.sleep(2)   # let InnoDB finish initialising
                return
            time.sleep(4)
        raise TimeoutError(
            f"MySQL container '{container}' did not become ready within {timeout}s"
        )

    def _stop_container(self, container: str) -> None:
        subprocess.run(["docker", "stop", container], capture_output=True)
        subprocess.run(["docker", "rm",   container], capture_output=True)

    def _docker_cpuset_args(self, cfg: Dict) -> List[str]:
        """Validate and return --cpuset-cpus / --cpuset-mems flags for docker run."""
        args = []
        for flag, key, label in (
            ("--cpuset-cpus", "mysql_cpus", "mysql_cpus"),
            ("--cpuset-mems", "mysql_mems", "mysql_mems"),
        ):
            value = str(cfg.get(key, "")).strip()
            if not value:
                continue
            # Accept: single int, range (N-M), or comma-separated mix thereof
            if not re.fullmatch(r"\d+(-\d+)?(,\d+(-\d+)?)*", value):
                raise ValueError(
                    f"Invalid {label}={value!r}. "
                    "Use comma-separated integers or ranges: '0', '0,1,2,3', '0-3', '0-3,8-11'."
                )
            args += [flag, value]
        return args

    # ── BaseWorkload interface ─────────────────────────────────────────────────

    def setup(self, config: BenchmarkConfig, work_dir: str) -> None:
        cfg  = self._cfg(config)
        port = int(cfg["mysql_port"])
        image = cfg["mysql_image"]

        os.makedirs(work_dir, exist_ok=True)

        # Remove any stale container left by a crashed previous run
        stale = self._read_container(work_dir)
        if stale:
            print(f"[sysbench_mysql] Removing stale container '{stale}'...")
            self._stop_container(stale)
            self._remove_container_file(work_dir)

        # Pull image
        print(f"[sysbench_mysql] Pulling {image}...")
        subprocess.run(["docker", "pull", image], check=True)

        # Start container
        container = f"sysbench-mysql-{os.getpid()}"
        cpuset_args = self._docker_cpuset_args(cfg)

        pin_parts = []
        if cfg.get("mysql_cpus"):
            pin_parts.append(f"cpuset-cpus={cfg['mysql_cpus']}")
        if cfg.get("mysql_mems"):
            pin_parts.append(f"cpuset-mems={cfg['mysql_mems']}")
        pin_info = f"  [{', '.join(pin_parts)}]" if pin_parts else ""

        print(
            f"[sysbench_mysql] Starting MySQL container '{container}' "
            f"(port {port}){pin_info}...",
            flush=True,
        )
        subprocess.run(
            [
                "docker", "run", "-d",
                "--name", container,
                "--label", "created-by=benchmark-toolkit",
                "-e", f"MYSQL_ROOT_PASSWORD={_MYSQL_PASSWORD}",
                "-e", f"MYSQL_DATABASE={_MYSQL_DB}",
                "-p", f"{port}:3306",
            ] + cpuset_args + [image],
            check=True,
        )
        self._write_container(work_dir, container)

        # Wait for MySQL
        self._wait_for_mysql(container, port)

        # Prepare sysbench data
        print(
            f"[sysbench_mysql] Preparing: test={cfg['test']}  "
            f"tables={cfg['tables']}  table_size={int(cfg['table_size']):,}  "
            f"threads={config.num_threads}",
            flush=True,
        )
        subprocess.run(
            self._sysbench_cmd(cfg, config.num_threads, "prepare"),
            check=True,
        )
        print("[sysbench_mysql] Setup complete.", flush=True)

    def build_command(self, config: BenchmarkConfig) -> List[str]:
        """Return the sysbench run command; taskset/numactl are prepended by the runner."""
        return self._sysbench_cmd(self._cfg(config), config.num_threads, "run")

    def parse_output(self, stdout: str, stderr: str, returncode: int) -> Dict[str, float]:
        metrics: Dict[str, float] = {}

        for line in stdout.splitlines():
            s = line.strip()

            # transactions:  10000  (166.59 per sec.)
            if s.startswith("transactions:"):
                m = re.search(r"\(([\d.]+)\s+per sec\.\)", s)
                if m:
                    metrics["transactions_per_sec"] = float(m.group(1))

            # queries:  200000  (3331.79 per sec.)
            elif s.startswith("queries:"):
                m = re.search(r"\(([\d.]+)\s+per sec\.\)", s)
                if m:
                    metrics["queries_per_sec"] = float(m.group(1))

            # ignored errors:  0  (0.00 per sec.)
            elif s.startswith("ignored errors:"):
                m = re.search(r"\(([\d.]+)\s+per sec\.\)", s)
                if m:
                    metrics["errors_per_sec"] = float(m.group(1))

            # total time:  60.0260s
            elif s.startswith("total time:"):
                try:
                    metrics["total_time_sec"] = float(s.split(":")[-1].strip().rstrip("s"))
                except ValueError:
                    pass

            # total number of events:  10000
            elif s.startswith("total number of events:"):
                try:
                    metrics["total_events"] = float(s.split(":")[-1].strip())
                except ValueError:
                    pass

            # Latency block — min / avg / max / 95th percentile
            elif s.startswith("min:"):
                try:
                    metrics["latency_min_ms"] = float(s.split(":")[-1].strip())
                except ValueError:
                    pass
            elif s.startswith("avg:"):
                try:
                    metrics["latency_avg_ms"] = float(s.split(":")[-1].strip())
                except ValueError:
                    pass
            elif s.startswith("max:"):
                try:
                    metrics["latency_max_ms"] = float(s.split(":")[-1].strip())
                except ValueError:
                    pass
            elif s.startswith("95th percentile:"):
                try:
                    metrics["latency_p95_ms"] = float(s.split(":")[-1].strip())
                except ValueError:
                    pass

        return metrics

    def teardown(self, config: BenchmarkConfig, work_dir: str) -> None:
        cfg       = self._cfg(config)
        container = self._read_container(work_dir)
        if not container:
            return

        print(f"[sysbench_mysql] Running sysbench cleanup...", flush=True)
        subprocess.run(
            self._sysbench_cmd(cfg, config.num_threads, "cleanup"),
            capture_output=True,
        )

        print(f"[sysbench_mysql] Stopping and removing container '{container}'...", flush=True)
        self._stop_container(container)
        self._remove_container_file(work_dir)
        print("[sysbench_mysql] Teardown complete.", flush=True)
