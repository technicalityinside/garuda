"""
benchmark_toolkit - Python workload automation toolkit.

Provides CPU topology detection, workload registration, benchmark running,
result collection, and scaling study utilities.
"""

from .system import detect_topology, SystemTopology, CoreInfo
from .config import BenchmarkConfig, ConfigPreset
from .base import BaseWorkload
from .registry import registry, WorkloadRegistry
from .runner import BenchmarkRunner, RunResult
from .collector import ResultsCollector
from .scaling import ScalingStudy

__all__ = [
    "detect_topology",
    "SystemTopology",
    "CoreInfo",
    "BenchmarkConfig",
    "ConfigPreset",
    "BaseWorkload",
    "registry",
    "WorkloadRegistry",
    "BenchmarkRunner",
    "RunResult",
    "ResultsCollector",
    "ScalingStudy",
]

__version__ = "1.0.0"
