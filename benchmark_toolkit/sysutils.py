"""
System utilities: package manager detection and installation.

PackageInstaller detects the host package manager and resolves logical
package names to their distro-specific names.  New distros are added by
extending PACKAGE_MANAGERS and PACKAGE_MAP.

Usage:
    from benchmark_toolkit.sysutils import PackageInstaller

    ok, msg = PackageInstaller.ensure_tools(
        ("gcc",  "gcc"),
        ("make", "make"),
    )
    if not ok:
        return False, f"Build deps unavailable: {msg}"
"""

import shutil
import subprocess
from typing import Dict, List, Optional, Tuple, Union


# ---------------------------------------------------------------------------
# Package manager descriptors
# ---------------------------------------------------------------------------

class _PM:
    """Descriptor for a single package manager."""
    def __init__(self, binary: str, install_cmd: List[str]):
        self.binary = binary
        # install_cmd must end right before the package list, e.g.
        # ["sudo", "apt-get", "install", "-y"]
        self.install_cmd = install_cmd

    def available(self) -> bool:
        return shutil.which(self.binary) is not None

    def install(self, packages: List[str]) -> "subprocess.CompletedProcess[str]":
        return subprocess.run(self.install_cmd + packages)


# Ordered list — first match wins.  Add new distros here.
PACKAGE_MANAGERS: List[_PM] = [
    _PM("apt-get", ["sudo", "apt-get", "install", "-y"]),
    _PM("apt",     ["sudo", "apt",     "install", "-y"]),
    _PM("dnf",     ["sudo", "dnf",     "install", "-y"]),
    _PM("yum",     ["sudo", "yum",     "install", "-y"]),
    _PM("pacman",  ["sudo", "pacman",  "-S", "--noconfirm"]),
    _PM("zypper",  ["sudo", "zypper",  "install", "-y"]),
    _PM("brew",    ["brew", "install"]),                    # macOS (no sudo)
]


# ---------------------------------------------------------------------------
# Package name map
# ---------------------------------------------------------------------------
# Maps a *logical* name to the real package name per package manager binary.
# Omitting a PM means the logical name is used as-is for that PM.
# A value may be a str (single package) or List[str] (multiple packages).
#
# To support a new distro: add its PM binary to PACKAGE_MANAGERS above,
# then add overrides here for any packages whose names differ.

PackageSpec = Union[str, List[str]]

PACKAGE_MAP: Dict[str, Dict[str, PackageSpec]] = {
    "gcc": {
        # name is "gcc" everywhere — listed explicitly for clarity
        "apt-get": "gcc",
        "apt":     "gcc",
        "dnf":     "gcc",
        "yum":     "gcc",
        "pacman":  "gcc",
        "zypper":  "gcc",
        "brew":    "gcc",
    },
    "make": {
        "apt-get": "make",
        "apt":     "make",
        "dnf":     "make",
        "yum":     "make",
        "pacman":  "make",
        "zypper":  "make",
        "brew":    "make",
    },
    "git": {
        "apt-get": "git",
        "apt":     "git",
        "dnf":     "git",
        "yum":     "git",
        "pacman":  "git",
        "zypper":  "git",
        "brew":    "git",
    },
    # build-essential is a convenience alias for apt; other PMs get the
    # individual packages.
    "build-essential": {
        "apt-get": "build-essential",
        "apt":     "build-essential",
        "dnf":     ["gcc", "gcc-c++", "make"],
        "yum":     ["gcc", "gcc-c++", "make"],
        "pacman":  "base-devel",
        "zypper":  ["gcc", "make"],
        "brew":    ["gcc", "make"],
    },
}


# ---------------------------------------------------------------------------
# PackageInstaller
# ---------------------------------------------------------------------------

class PackageInstaller:
    """Detect the host package manager and install packages."""

    @staticmethod
    def detect() -> Optional[_PM]:
        """Return the first available package manager, or None."""
        for pm in PACKAGE_MANAGERS:
            if pm.available():
                return pm
        return None

    @classmethod
    def _resolve(cls, pm: _PM, logical_name: str) -> List[str]:
        """Resolve a logical package name to a list of real package names."""
        overrides = PACKAGE_MAP.get(logical_name, {})
        resolved = overrides.get(pm.binary, logical_name)
        return resolved if isinstance(resolved, list) else [resolved]

    @classmethod
    def install(cls, *logical_names: str) -> Tuple[bool, str]:
        """
        Install one or more packages (by logical name) using the detected PM.
        Returns (success, message).
        """
        pm = cls.detect()
        if pm is None:
            pkgs = ", ".join(logical_names)
            return False, (
                f"No supported package manager found. "
                f"Install manually: {pkgs}"
            )

        actual: List[str] = []
        for name in logical_names:
            actual.extend(cls._resolve(pm, name))

        # Deduplicate while preserving order
        seen: set = set()
        actual = [p for p in actual if not (p in seen or seen.add(p))]  # type: ignore[func-returns-value]

        print(f"  Installing {', '.join(actual)} via {pm.binary} ...")
        result = pm.install(actual)
        if result.returncode == 0:
            return True, f"Installed {', '.join(actual)} via {pm.binary}"
        return False, (
            f"{pm.binary} install failed (exit {result.returncode}). "
            f"Packages: {', '.join(actual)}"
        )

    @classmethod
    def ensure_tools(
        cls, *tool_package_pairs: Tuple[str, str]
    ) -> Tuple[bool, str]:
        """
        For each (tool_binary, logical_package) pair, check whether the tool
        is already in PATH.  Any that are missing are installed together in a
        single package-manager invocation.

        Example::

            ok, msg = PackageInstaller.ensure_tools(
                ("gcc",  "gcc"),
                ("make", "make"),
            )

        Returns (all_available, message).
        """
        missing_tools: List[str] = []
        missing_pkgs:  List[str] = []
        for tool, pkg in tool_package_pairs:
            if not shutil.which(tool):
                missing_tools.append(tool)
                missing_pkgs.append(pkg)

        if not missing_tools:
            return True, "All tools already available"

        print(f"  Missing: {', '.join(missing_tools)}")
        ok, msg = cls.install(*missing_pkgs)
        if not ok:
            return False, msg

        # Verify
        still_missing = [t for t in missing_tools if not shutil.which(t)]
        if still_missing:
            return False, (
                f"Still missing after install: {', '.join(still_missing)}"
            )

        return True, f"Installed: {', '.join(missing_tools)}"

    @staticmethod
    def pm_name() -> str:
        """Return the name of the detected package manager, or 'unknown'."""
        pm = PackageInstaller.detect()
        return pm.binary if pm else "unknown"
