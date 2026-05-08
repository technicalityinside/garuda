"""
CPU and NUMA topology detection.

Primary source: /sys/devices/system/cpu/cpuN/topology/
Fallback: parse lscpu output when sysfs is unavailable.
"""

import os
import re
import subprocess
from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class CoreInfo:
    core_id: int            # physical core id
    socket_id: int
    numa_node: int
    logical_cpus: List[int]  # e.g. [0, 16] for core with HT pair


@dataclass
class SystemTopology:
    total_logical_cpus: int
    total_physical_cores: int
    total_sockets: int
    total_numa_nodes: int
    cores: List[CoreInfo]                        # one entry per physical core
    socket_cpus: Dict[int, List[int]]            # socket_id -> all logical CPUs
    socket_physical_cpus: Dict[int, List[int]]   # socket_id -> first logical CPU per core
    numa_cpus: Dict[int, List[int]]              # numa_node -> logical CPUs

    def get_n_cpus(self, n: int, socket: int = 0, use_smt: bool = False) -> List[int]:
        """
        Return n CPU ids from the given socket.
        Prefer physical cores first (first logical CPU of each core).
        If use_smt or more CPUs needed than physical cores, add SMT siblings.
        """
        physical = self.socket_physical_cpus.get(socket, [])
        all_logical = self.socket_cpus.get(socket, [])

        if n <= 0:
            return []

        if not use_smt and n <= len(physical):
            return sorted(physical[:n])

        # Need SMT siblings or use_smt requested
        # Build ordered list: physical cores first, then their SMT siblings
        ordered: List[int] = []
        # Map from first-logical-cpu -> full core info
        socket_cores = [c for c in self.cores if c.socket_id == socket]
        # Sort by core_id for deterministic ordering
        socket_cores_sorted = sorted(socket_cores, key=lambda c: c.core_id)

        # First pass: add first logical CPU (physical core representative)
        for core in socket_cores_sorted:
            if core.logical_cpus:
                ordered.append(core.logical_cpus[0])

        # Second pass: add SMT siblings
        for core in socket_cores_sorted:
            for cpu in core.logical_cpus[1:]:
                ordered.append(cpu)

        # If still not enough, fall back to all logical
        if n > len(ordered):
            # Include any remaining logical CPUs not yet in ordered
            all_set = set(all_logical)
            ordered_set = set(ordered)
            extras = sorted(all_set - ordered_set)
            ordered.extend(extras)

        return sorted(ordered[:n])

    def get_nc_mt_cpus(self, n_cores: int, threads_per_core: int, socket: int = 0) -> List[int]:
        """
        Return CPUs for exactly n_cores physical cores with threads_per_core threads each.
        Cores are selected in core_id order; threads are the first threads_per_core
        logical siblings of each core (physical thread first, then SMT sibling).
        """
        socket_cores = sorted(
            [c for c in self.cores if c.socket_id == socket],
            key=lambda c: c.core_id,
        )
        selected = socket_cores[:n_cores]
        cpus: List[int] = []
        for core in selected:
            cpus.extend(core.logical_cpus[:threads_per_core])
        return sorted(cpus)

    def threads_per_core(self, socket: int = 0) -> int:
        """Return the SMT width (threads per physical core) on the given socket."""
        socket_cores = [c for c in self.cores if c.socket_id == socket]
        if not socket_cores:
            return 1
        return max(len(c.logical_cpus) for c in socket_cores)

    def get_all_physical(self, socket: int = 0) -> List[int]:
        """First logical CPU of each core on the socket."""
        return sorted(self.socket_physical_cpus.get(socket, []))

    def get_all_logical(self, socket: int = 0) -> List[int]:
        """All logical CPUs on the socket."""
        return sorted(self.socket_cpus.get(socket, []))


def _parse_cpu_list(s: str) -> List[int]:
    """Parse a CPU list string like '0-3,8,10-12' into a list of ints."""
    cpus: List[int] = []
    s = s.strip()
    if not s:
        return cpus
    for part in s.split(","):
        part = part.strip()
        if "-" in part:
            lo, hi = part.split("-", 1)
            cpus.extend(range(int(lo), int(hi) + 1))
        else:
            cpus.append(int(part))
    return cpus


def _read_sysfs_int(path: str) -> Optional[int]:
    try:
        with open(path) as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return None


def _read_sysfs_str(path: str) -> Optional[str]:
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return None


def _detect_numa_node_for_cpu(cpu_id: int) -> int:
    """Find NUMA node for a given logical CPU by scanning /sys/devices/system/node/."""
    node_base = "/sys/devices/system/node"
    if not os.path.isdir(node_base):
        return 0
    for entry in os.scandir(node_base):
        if not entry.name.startswith("node"):
            continue
        try:
            node_id = int(entry.name[4:])
        except ValueError:
            continue
        cpulist_path = os.path.join(entry.path, "cpulist")
        cpulist_str = _read_sysfs_str(cpulist_path)
        if cpulist_str is None:
            continue
        if cpu_id in _parse_cpu_list(cpulist_str):
            return node_id
    return 0


def _detect_from_sysfs() -> Optional[SystemTopology]:
    """Try to build topology from /sys/devices/system/cpu/."""
    cpu_base = "/sys/devices/system/cpu"
    if not os.path.isdir(cpu_base):
        return None

    # Enumerate online CPUs
    online_path = os.path.join(cpu_base, "online")
    online_str = _read_sysfs_str(online_path)
    if online_str:
        cpu_ids = _parse_cpu_list(online_str)
    else:
        # Fallback: scan directories
        cpu_ids = []
        try:
            for entry in os.scandir(cpu_base):
                if re.match(r"cpu\d+$", entry.name):
                    cpu_ids.append(int(entry.name[3:]))
        except OSError:
            return None

    if not cpu_ids:
        return None

    # Build NUMA node -> cpulist mapping from sysfs
    numa_cpus: Dict[int, List[int]] = {}
    node_base = "/sys/devices/system/node"
    if os.path.isdir(node_base):
        try:
            for entry in os.scandir(node_base):
                if not entry.name.startswith("node"):
                    continue
                try:
                    node_id = int(entry.name[4:])
                except ValueError:
                    continue
                cpulist_path = os.path.join(entry.path, "cpulist")
                cpulist_str = _read_sysfs_str(cpulist_path)
                if cpulist_str:
                    numa_cpus[node_id] = _parse_cpu_list(cpulist_str)
        except OSError:
            pass
    if not numa_cpus:
        numa_cpus[0] = list(cpu_ids)

    # Build cpu -> numa_node reverse map
    cpu_to_numa: Dict[int, int] = {}
    for node_id, cpus in numa_cpus.items():
        for cpu in cpus:
            cpu_to_numa[cpu] = node_id

    # Key: (socket_id, core_id) -> CoreInfo
    core_map: Dict[tuple, CoreInfo] = {}

    for cpu_id in sorted(cpu_ids):
        topo_dir = os.path.join(cpu_base, f"cpu{cpu_id}", "topology")
        if not os.path.isdir(topo_dir):
            # cpu0 may not have topology dir on some systems; treat as socket 0, core 0
            socket_id = 0
            core_id = cpu_id
        else:
            socket_id = _read_sysfs_int(os.path.join(topo_dir, "physical_package_id"))
            if socket_id is None:
                socket_id = 0
            core_id = _read_sysfs_int(os.path.join(topo_dir, "core_id"))
            if core_id is None:
                core_id = cpu_id

        numa_node = cpu_to_numa.get(cpu_id, 0)
        key = (socket_id, core_id)

        if key not in core_map:
            core_map[key] = CoreInfo(
                core_id=core_id,
                socket_id=socket_id,
                numa_node=numa_node,
                logical_cpus=[cpu_id],
            )
        else:
            if cpu_id not in core_map[key].logical_cpus:
                core_map[key].logical_cpus.append(cpu_id)

    # Sort logical_cpus within each core
    for ci in core_map.values():
        ci.logical_cpus.sort()

    cores = sorted(core_map.values(), key=lambda c: (c.socket_id, c.core_id))

    # Build socket_cpus and socket_physical_cpus
    socket_cpus: Dict[int, List[int]] = {}
    socket_physical_cpus: Dict[int, List[int]] = {}
    for ci in cores:
        sid = ci.socket_id
        if sid not in socket_cpus:
            socket_cpus[sid] = []
            socket_physical_cpus[sid] = []
        socket_cpus[sid].extend(ci.logical_cpus)
        socket_physical_cpus[sid].append(ci.logical_cpus[0])

    for sid in socket_cpus:
        socket_cpus[sid].sort()
        socket_physical_cpus[sid].sort()

    return SystemTopology(
        total_logical_cpus=len(cpu_ids),
        total_physical_cores=len(cores),
        total_sockets=len(socket_cpus),
        total_numa_nodes=len(numa_cpus),
        cores=cores,
        socket_cpus=socket_cpus,
        socket_physical_cpus=socket_physical_cpus,
        numa_cpus=numa_cpus,
    )


def _detect_from_lscpu() -> SystemTopology:
    """
    Fallback: parse lscpu output to build topology.
    This is less precise but works when sysfs is unavailable.
    """
    try:
        result = subprocess.run(
            ["lscpu", "--parse=CPU,Core,Socket,Node"],
            capture_output=True, text=True, timeout=10,
        )
        output = result.stdout
    except (FileNotFoundError, subprocess.TimeoutExpired):
        # Last resort: return a minimal single-core topology
        return SystemTopology(
            total_logical_cpus=1,
            total_physical_cores=1,
            total_sockets=1,
            total_numa_nodes=1,
            cores=[CoreInfo(core_id=0, socket_id=0, numa_node=0, logical_cpus=[0])],
            socket_cpus={0: [0]},
            socket_physical_cpus={0: [0]},
            numa_cpus={0: [0]},
        )

    core_map: Dict[tuple, CoreInfo] = {}
    numa_cpus: Dict[int, List[int]] = {}

    for line in output.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(",")
        if len(parts) < 4:
            continue
        try:
            cpu_id = int(parts[0])
            core_id = int(parts[1])
            socket_id = int(parts[2])
            # Node may be empty string on non-NUMA systems
            node_str = parts[3].strip()
            numa_node = int(node_str) if node_str else 0
        except ValueError:
            continue

        key = (socket_id, core_id)
        if key not in core_map:
            core_map[key] = CoreInfo(
                core_id=core_id,
                socket_id=socket_id,
                numa_node=numa_node,
                logical_cpus=[cpu_id],
            )
        else:
            if cpu_id not in core_map[key].logical_cpus:
                core_map[key].logical_cpus.append(cpu_id)

        if numa_node not in numa_cpus:
            numa_cpus[numa_node] = []
        if cpu_id not in numa_cpus[numa_node]:
            numa_cpus[numa_node].append(cpu_id)

    if not core_map:
        # Complete fallback
        return SystemTopology(
            total_logical_cpus=1,
            total_physical_cores=1,
            total_sockets=1,
            total_numa_nodes=1,
            cores=[CoreInfo(core_id=0, socket_id=0, numa_node=0, logical_cpus=[0])],
            socket_cpus={0: [0]},
            socket_physical_cpus={0: [0]},
            numa_cpus={0: [0]},
        )

    for ci in core_map.values():
        ci.logical_cpus.sort()

    cores = sorted(core_map.values(), key=lambda c: (c.socket_id, c.core_id))

    socket_cpus: Dict[int, List[int]] = {}
    socket_physical_cpus: Dict[int, List[int]] = {}
    for ci in cores:
        sid = ci.socket_id
        if sid not in socket_cpus:
            socket_cpus[sid] = []
            socket_physical_cpus[sid] = []
        socket_cpus[sid].extend(ci.logical_cpus)
        socket_physical_cpus[sid].append(ci.logical_cpus[0])

    for sid in socket_cpus:
        socket_cpus[sid].sort()
        socket_physical_cpus[sid].sort()

    if not numa_cpus:
        all_cpus = sorted(set(c for cpus in socket_cpus.values() for c in cpus))
        numa_cpus[0] = all_cpus

    return SystemTopology(
        total_logical_cpus=sum(len(ci.logical_cpus) for ci in cores),
        total_physical_cores=len(cores),
        total_sockets=len(socket_cpus),
        total_numa_nodes=len(numa_cpus),
        cores=cores,
        socket_cpus=socket_cpus,
        socket_physical_cpus=socket_physical_cpus,
        numa_cpus=numa_cpus,
    )


def detect_topology() -> SystemTopology:
    """Detect CPU and NUMA topology. Uses sysfs; falls back to lscpu."""
    topo = _detect_from_sysfs()
    if topo is not None:
        return topo
    return _detect_from_lscpu()
