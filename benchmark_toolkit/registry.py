"""
Workload registry with auto-discovery.

Scans a workloads/ directory, imports each .py file, and registers
any subclasses of BaseWorkload that have a non-empty `name` attribute.
Import errors are caught and reported as warnings rather than crashes.
"""

import importlib.util
import inspect
import os
import sys
import warnings
from typing import Dict, List, Type

from .base import BaseWorkload


class WorkloadRegistry:
    def __init__(self):
        self._workloads: Dict[str, Type[BaseWorkload]] = {}

    def register(self, cls: Type[BaseWorkload]) -> None:
        """Register a workload class. Overwrites if name already registered."""
        if not inspect.isclass(cls) or not issubclass(cls, BaseWorkload):
            raise TypeError(f"{cls} is not a subclass of BaseWorkload")
        if not cls.name:
            raise ValueError(f"Workload class {cls.__name__} has an empty name")
        self._workloads[cls.name] = cls

    def discover(self, workloads_dir: str) -> None:
        """
        Import all .py files in workloads_dir.
        Registers any BaseWorkload subclasses found with a non-empty name.
        Skips files that fail to import (with a warning).
        """
        if not os.path.isdir(workloads_dir):
            warnings.warn(f"Workloads directory not found: {workloads_dir}")
            return

        for filename in sorted(os.listdir(workloads_dir)):
            if not filename.endswith(".py") or filename.startswith("_"):
                continue

            filepath = os.path.join(workloads_dir, filename)
            module_name = f"_discovered_workloads.{filename[:-3]}"

            try:
                spec = importlib.util.spec_from_file_location(module_name, filepath)
                if spec is None or spec.loader is None:
                    warnings.warn(f"Could not load spec for {filepath}, skipping.")
                    continue
                module = importlib.util.module_from_spec(spec)
                # Add to sys.modules before exec so relative imports work
                sys.modules[module_name] = module
                spec.loader.exec_module(module)
            except Exception as exc:
                warnings.warn(
                    f"Failed to import workload file {filepath}: {exc}",
                    stacklevel=2,
                )
                # Remove partially loaded module
                sys.modules.pop(module_name, None)
                continue

            # Scan module for BaseWorkload subclasses
            for attr_name in dir(module):
                try:
                    obj = getattr(module, attr_name)
                except Exception:
                    continue
                if (
                    inspect.isclass(obj)
                    and issubclass(obj, BaseWorkload)
                    and obj is not BaseWorkload
                    and obj.name  # non-empty name
                ):
                    try:
                        self.register(obj)
                    except Exception as exc:
                        warnings.warn(
                            f"Could not register {obj.__name__} from {filepath}: {exc}",
                            stacklevel=2,
                        )

    def get(self, name: str) -> BaseWorkload:
        """Instantiate and return a workload by name."""
        if name not in self._workloads:
            available = ", ".join(sorted(self._workloads.keys())) or "(none)"
            raise KeyError(
                f"Workload {name!r} not found. Available workloads: {available}"
            )
        return self._workloads[name]()

    def list_all(self) -> List[Dict]:
        """Return list of {name, description, version} dicts, sorted by name."""
        result = []
        for name, cls in sorted(self._workloads.items()):
            result.append({
                "name": name,
                "description": cls.description,
                "version": cls.version,
            })
        return result

    def __contains__(self, name: str) -> bool:
        return name in self._workloads

    def __len__(self) -> int:
        return len(self._workloads)


# Module-level singleton registry
registry = WorkloadRegistry()
