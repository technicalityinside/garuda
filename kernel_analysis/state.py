"""State management for a kernel analysis session (persisted across reboots)."""

import json
import os
from typing import Dict, List, Optional

DEFAULT_STATE_FILE = "/var/lib/garuda/kernel_analysis.json"


class KernelEntry:
    """Tracks progress for a single kernel version under analysis."""

    VALID_STATUSES = ("pending", "installed", "rebooting", "benchmarking",
                      "benchmarked", "pushed", "failed", "skipped")

    def __init__(self, version: str, status: str = "pending",
                 run_ids: List[str] = None, scores: Dict[str, float] = None,
                 error: Optional[str] = None, started_at: Optional[str] = None,
                 completed_at: Optional[str] = None):
        self.version = version
        self.status = status
        self.run_ids = run_ids or []
        self.scores = scores or {}
        self.error = error
        self.started_at = started_at
        self.completed_at = completed_at

    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "status": self.status,
            "run_ids": self.run_ids,
            "scores": self.scores,
            "error": self.error,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "KernelEntry":
        return cls(**{k: v for k, v in d.items() if k in cls.__init__.__code__.co_varnames})

    def is_terminal(self) -> bool:
        return self.status in ("pushed", "failed", "skipped")


class AnalysisSession:
    """Full state for one kernel analysis run, persisted to JSON."""

    def __init__(self, *, session_id: str, kernels: List[KernelEntry],
                 workloads: List[str], config: Optional[str], iterations: int,
                 push_url: Optional[str], api_key: str, system_name: str,
                 kernel_config_label: str, original_kernel: str,
                 state_file: str = DEFAULT_STATE_FILE, status: str = "running",
                 started_at: Optional[str] = None, completed_at: Optional[str] = None,
                 workload_args: Optional[Dict] = None):
        self.session_id = session_id
        self.kernels = kernels
        self.workloads = workloads
        self.config = config
        self.iterations = iterations
        self.push_url = push_url
        self.api_key = api_key
        self.system_name = system_name
        self.kernel_config_label = kernel_config_label
        self.original_kernel = original_kernel
        self.state_file = state_file
        self.status = status
        self.started_at = started_at
        self.completed_at = completed_at
        self.workload_args = workload_args or {}

    # ── Persistence ──────────────────────────────────────────────────────────

    def save(self) -> None:
        os.makedirs(os.path.dirname(self.state_file), exist_ok=True)
        tmp = self.state_file + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self._to_dict(), f, indent=2)
        os.replace(tmp, self.state_file)

    def _to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "kernels": [k.to_dict() for k in self.kernels],
            "workloads": self.workloads,
            "config": self.config,
            "iterations": self.iterations,
            "push_url": self.push_url,
            "api_key": self.api_key,
            "system_name": self.system_name,
            "kernel_config_label": self.kernel_config_label,
            "original_kernel": self.original_kernel,
            "state_file": self.state_file,
            "status": self.status,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "workload_args": self.workload_args,
        }

    @classmethod
    def load(cls, path: str) -> "AnalysisSession":
        with open(path) as f:
            data = json.load(f)
        kernels_raw = data.pop("kernels")
        kernels = [KernelEntry.from_dict(k) for k in kernels_raw]
        return cls(kernels=kernels, **data)

    # ── Queries ───────────────────────────────────────────────────────────────

    def current_rebooting(self) -> Optional[KernelEntry]:
        """Return the kernel entry with status 'rebooting', or None."""
        for k in self.kernels:
            if k.status == "rebooting":
                return k
        return None

    def next_pending(self) -> Optional[KernelEntry]:
        """Return the first kernel entry with status 'pending', or None."""
        for k in self.kernels:
            if k.status == "pending":
                return k
        return None

    def all_terminal(self) -> bool:
        """True when every kernel entry has reached a terminal state."""
        return all(k.is_terminal() for k in self.kernels)

    def summary_rows(self) -> List[List[str]]:
        """Return rows for a status table [[version, status, run_ids, error]]."""
        rows = []
        for k in self.kernels:
            rows.append([
                k.version,
                k.status,
                ", ".join(k.run_ids) if k.run_ids else "—",
                k.error or "—",
            ])
        return rows
