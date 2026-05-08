"""
Benchmark configuration dataclass and preset factory.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class BenchmarkConfig:
    name: str
    num_threads: int
    cpu_list: List[int]                              # CPUs to pin to
    numa_nodes: List[int] = field(default_factory=list)
    env_vars: Dict[str, str] = field(default_factory=dict)
    iterations: int = 1
    timeout: int = 3600
    use_taskset: bool = True
    use_numactl: bool = False
    description: str = ""
    workload_args: Dict[str, Any] = field(default_factory=dict)
    scaling_tag: str = ""                            # tag for scaling studies


class ConfigPreset:
    """Factory: given a SystemTopology, return named BenchmarkConfig objects."""

    @staticmethod
    def single_core(topo) -> BenchmarkConfig:
        cpus = topo.get_n_cpus(1)
        return BenchmarkConfig(
            name="single_core",
            num_threads=1,
            cpu_list=cpus,
            description="1 thread on 1 physical core",
            scaling_tag="single_core",
        )

    @staticmethod
    def dual_core(topo) -> BenchmarkConfig:
        n = 2
        cpus = topo.get_n_cpus(n)
        return BenchmarkConfig(
            name="dual_core",
            num_threads=n,
            cpu_list=cpus,
            description="2 threads on 2 physical cores",
            scaling_tag="dual_core",
        )

    @staticmethod
    def quad_core(topo) -> BenchmarkConfig:
        n = 4
        cpus = topo.get_n_cpus(n)
        return BenchmarkConfig(
            name="quad_core",
            num_threads=n,
            cpu_list=cpus,
            description="4 threads on 4 physical cores",
            scaling_tag="quad_core",
        )

    @staticmethod
    def octa_core(topo) -> BenchmarkConfig:
        n = 8
        cpus = topo.get_n_cpus(n)
        return BenchmarkConfig(
            name="octa_core",
            num_threads=n,
            cpu_list=cpus,
            description="8 threads on 8 physical cores",
            scaling_tag="octa_core",
        )

    @staticmethod
    def half_socket(topo) -> BenchmarkConfig:
        n = max(1, topo.total_physical_cores // 2)
        cpus = topo.get_n_cpus(n)
        return BenchmarkConfig(
            name="half_socket",
            num_threads=n,
            cpu_list=cpus,
            description=f"Half socket: {n} physical cores on socket 0",
            scaling_tag="half_socket",
        )

    @staticmethod
    def full_socket(topo) -> BenchmarkConfig:
        cpus = topo.get_all_physical(socket=0)
        n = len(cpus)
        return BenchmarkConfig(
            name="full_socket",
            num_threads=n,
            cpu_list=cpus,
            description=f"All {n} physical cores on socket 0",
            scaling_tag="full_socket",
        )

    @staticmethod
    def full_socket_smt(topo) -> BenchmarkConfig:
        cpus = topo.get_all_logical(socket=0)
        n = len(cpus)
        return BenchmarkConfig(
            name="full_socket_smt",
            num_threads=n,
            cpu_list=cpus,
            description=f"All {n} logical CPUs on socket 0 (SMT enabled)",
            scaling_tag="full_socket_smt",
        )

    @staticmethod
    def nc_mt(topo, n_cores: int, threads_per_core: int, socket: int = 0) -> BenchmarkConfig:
        """
        Create a config for n_cores physical cores with threads_per_core threads each.
        Name follows the NcMT convention, e.g. 4c8t = 4 cores × 2 threads/core.
        """
        cpus = topo.get_nc_mt_cpus(n_cores, threads_per_core, socket=socket)
        n_threads = len(cpus)
        name = f"{n_cores}c{n_threads}t"
        tpc_str = f"{threads_per_core} thread{'s' if threads_per_core > 1 else ''}/core"
        return BenchmarkConfig(
            name=name,
            num_threads=n_threads,
            cpu_list=cpus,
            description=f"{n_cores} core{'s' if n_cores > 1 else ''}, {tpc_str}",
            scaling_tag=name,
        )

    @staticmethod
    def nc_mt_presets(topo, socket: int = 0) -> Dict[str, "BenchmarkConfig"]:
        """
        Generate the full NcMT grid for the given topology.
        For each power-of-2 core count up to total_physical_cores, produces:
          - NcNt  (1 thread/core, no SMT)
          - Nc(N*smt_width)t  (all threads/core, with SMT) — only when SMT width > 1
        """
        smt_width = topo.threads_per_core(socket=socket)
        n_physical = topo.total_physical_cores

        core_counts: List[int] = []
        n = 1
        while n <= n_physical:
            core_counts.append(n)
            n *= 2
        if core_counts[-1] != n_physical:
            core_counts.append(n_physical)

        presets: Dict[str, BenchmarkConfig] = {}
        for nc in core_counts:
            # Physical only (1 thread/core)
            cfg_phys = ConfigPreset.nc_mt(topo, nc, 1, socket=socket)
            presets[cfg_phys.name] = cfg_phys
            # SMT (all threads/core) — skip if SMT width == 1
            if smt_width > 1:
                cfg_smt = ConfigPreset.nc_mt(topo, nc, smt_width, socket=socket)
                presets[cfg_smt.name] = cfg_smt

        return presets

    @staticmethod
    def custom(name: str, num_threads: int, cpu_list: List[int], **kwargs) -> BenchmarkConfig:
        return BenchmarkConfig(
            name=name,
            num_threads=num_threads,
            cpu_list=cpu_list,
            description=kwargs.get("description", f"Custom: {num_threads} threads on {cpu_list}"),
            **{k: v for k, v in kwargs.items() if k != "description" and k in BenchmarkConfig.__dataclass_fields__},
        )

    @staticmethod
    def all_presets(topo) -> Dict[str, "BenchmarkConfig"]:
        """
        Return dict of all standard named presets.
        NcMT presets (1c1t, 1c2t, 2c2t, 2c4t, ...) are listed first,
        followed by legacy named aliases.
        """
        presets: Dict[str, BenchmarkConfig] = {}

        # NcMT grid
        try:
            presets.update(ConfigPreset.nc_mt_presets(topo))
        except Exception:
            pass

        # Legacy named aliases (kept for backward compatibility)
        for factory in [
            ConfigPreset.single_core,
            ConfigPreset.dual_core,
            ConfigPreset.quad_core,
            ConfigPreset.octa_core,
            ConfigPreset.half_socket,
            ConfigPreset.full_socket,
            ConfigPreset.full_socket_smt,
        ]:
            try:
                cfg = factory(topo)
                if cfg.cpu_list and cfg.name not in presets:
                    presets[cfg.name] = cfg
            except Exception:
                pass

        return presets

    @staticmethod
    def scaling_configs(
        topo,
        mode: str = "powers_of_2",
        max_threads: Optional[int] = None,
        use_smt: bool = False,
        custom_list: Optional[List[int]] = None,
    ) -> List[BenchmarkConfig]:
        """
        Generate a list of configs for a scaling study.

        mode: 'powers_of_2' -> [1, 2, 4, 8, 16, ...]
              'linear'       -> [1, 2, 3, ..., max_threads]
              'custom'       -> use custom_list directly
        """
        total_logical = topo.total_logical_cpus
        total_physical = topo.total_physical_cores
        cap = max_threads if max_threads is not None else (total_logical if use_smt else total_physical)

        if mode == "powers_of_2":
            counts: List[int] = []
            n = 1
            while n <= cap:
                counts.append(n)
                n *= 2
            # Include cap if it wasn't hit exactly
            if counts and counts[-1] != cap:
                counts.append(cap)
        elif mode == "linear":
            counts = list(range(1, cap + 1))
        elif mode == "custom":
            if custom_list is None:
                raise ValueError("custom_list must be provided when mode='custom'")
            counts = [c for c in custom_list if c <= cap]
        else:
            raise ValueError(f"Unknown mode: {mode!r}. Expected 'powers_of_2', 'linear', or 'custom'.")

        configs: List[BenchmarkConfig] = []
        for n in counts:
            cpus = topo.get_n_cpus(n, socket=0, use_smt=use_smt)
            cfg = BenchmarkConfig(
                name=f"scale_{n}t",
                num_threads=n,
                cpu_list=cpus,
                description=f"Scaling config: {n} thread{'s' if n > 1 else ''}",
                scaling_tag=f"scale_{n}t",
            )
            configs.append(cfg)

        return configs
