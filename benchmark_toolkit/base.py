"""
Abstract base class for all workloads.
"""

import os
from abc import ABC, abstractmethod
from typing import Dict, List, Tuple

from .config import BenchmarkConfig


class BaseWorkload(ABC):
    name: str = ""           # unique identifier, used as CLI name
    description: str = ""
    version: str = "1.0"

    @abstractmethod
    def validate(self) -> Tuple[bool, str]:
        """
        Return (ok, message).
        Check if required binaries/dependencies exist.
        """
        ...

    def setup(self, config: BenchmarkConfig, work_dir: str) -> None:
        """
        Optional: compile binaries, download data files, write helper scripts, etc.
        Called once before all iterations.
        """
        pass

    @abstractmethod
    def build_command(self, config: BenchmarkConfig) -> List[str]:
        """
        Return argv list for subprocess.
        Do NOT include taskset/numactl here — the runner prepends those.
        """
        ...

    def get_env(self, config: BenchmarkConfig) -> Dict[str, str]:
        """
        Return environment variable overrides.
        These are merged on top of os.environ by the runner.
        """
        return {
            "OMP_NUM_THREADS": str(config.num_threads),
            "GOMP_NUM_THREADS": str(config.num_threads),
            **config.env_vars,
        }

    @abstractmethod
    def parse_output(self, stdout: str, stderr: str, returncode: int) -> Dict[str, float]:
        """
        Parse raw subprocess output into a metrics dict.
        Example: {"events_per_sec": 1234.5, "total_time_sec": 10.0}

        Return an empty dict on parse failure; the runner will mark the run as failed.
        """
        ...

    def teardown(self, config: BenchmarkConfig, work_dir: str) -> None:
        """
        Optional: cleanup after all iterations (remove temp files, etc.).
        """
        pass

    def default_workload_args(self) -> Dict:
        """
        Default workload-specific arguments.
        These are overridden by values in BenchmarkConfig.workload_args.
        """
        return {}
