"""Kernel installation, GRUB manipulation, and reboot helpers."""

import os
import re
import subprocess
from typing import Optional, Tuple

# ── Current kernel ────────────────────────────────────────────────────────────

def current_kernel() -> str:
    """Return the version string of the currently running kernel."""
    try:
        return subprocess.run(
            ["uname", "-r"], capture_output=True, text=True, timeout=5
        ).stdout.strip()
    except Exception:
        return "unknown"


# ── Kernel installation ───────────────────────────────────────────────────────

def is_installed(kernel_ver: str) -> bool:
    """True if /boot/vmlinuz-{kernel_ver} exists."""
    return os.path.exists(f"/boot/vmlinuz-{kernel_ver}")


def install(kernel_ver: str) -> Tuple[bool, str]:
    """
    Install kernel via apt-get. Tries image+headers first, then image only.
    Returns (success, message).
    """
    env = {**os.environ, "DEBIAN_FRONTEND": "noninteractive"}
    image_pkg = f"linux-image-{kernel_ver}"
    headers_pkg = f"linux-headers-{kernel_ver}"

    print(f"    apt-get install {image_pkg} {headers_pkg} ...")
    r = subprocess.run(
        ["apt-get", "install", "-y", "--no-install-recommends", image_pkg, headers_pkg],
        env=env,
    )
    if r.returncode == 0:
        return True, f"Installed {image_pkg} and headers"

    print(f"    Headers not found, retrying image only ...")
    r = subprocess.run(
        ["apt-get", "install", "-y", "--no-install-recommends", image_pkg],
        env=env,
    )
    if r.returncode == 0:
        return True, f"Installed {image_pkg} (headers unavailable)"

    return False, f"apt-get install {image_pkg} failed (exit {r.returncode})"


def list_available_kernels(pattern: str = "linux-image-") -> list:
    """
    Return apt-cache search results for available kernel images.
    Useful for showing the user what versions can be installed.
    """
    try:
        out = subprocess.run(
            ["apt-cache", "search", pattern],
            capture_output=True, text=True, timeout=30,
        ).stdout
        pkgs = []
        for line in out.splitlines():
            pkg = line.split()[0] if line.strip() else ""
            if re.match(r"linux-image-\d", pkg):
                ver = pkg[len("linux-image-"):]
                pkgs.append(ver)
        return sorted(pkgs)
    except Exception:
        return []


# ── GRUB manipulation ─────────────────────────────────────────────────────────

def _find_grub_entry(kernel_ver: str,
                     grub_cfg: str = "/boot/grub/grub.cfg") -> Optional[str]:
    """
    Parse grub.cfg and return the menu path suitable for grub-reboot.

    For a kernel in the main menu:
        "Ubuntu, with Linux 6.8.0-55-generic"
    For a kernel inside a submenu:
        "Advanced options for Ubuntu>Ubuntu, with Linux 6.8.0-55-generic"
    """
    if not os.path.exists(grub_cfg):
        return None

    try:
        content = open(grub_cfg, errors="replace").read()
    except OSError:
        return None

    # Tokenise: pick up submenu/menuentry titles and closing braces
    token_re = re.compile(
        r'(?:submenu|menuentry)\s+["\']([^"\']+)["\']|(?<!\w)\}'
    )
    keyword_re = re.compile(r'(submenu|menuentry)\s+["\']([^"\']+)["\']')

    submenu_stack: list = []
    depth: int = 0  # track { } depth to know when submenu closes
    # We need to walk character by character for depth, but let's do a simpler
    # two-pass: first find all entries, then see which submenu they're under.

    # Simpler approach: scan for submenu/menuentry lines linearly, track depth.
    lines = content.splitlines()
    submenu_path: list = []
    brace_depth: list = [0]  # brace depth at each submenu level

    def current_path(entry_title: str) -> str:
        if submenu_path:
            return ">".join(submenu_path) + ">" + entry_title
        return entry_title

    for line in lines:
        stripped = line.strip()

        m = keyword_re.match(stripped)
        if m:
            keyword = m.group(1)
            title = m.group(2)

            if keyword == "submenu":
                submenu_path.append(title)
                brace_depth.append(0)
            elif keyword == "menuentry":
                if kernel_ver in title:
                    return current_path(title)

        # Count braces to track submenu nesting
        opens = stripped.count("{")
        closes = stripped.count("}")
        if brace_depth:
            brace_depth[-1] += opens - closes
            # Pop submenu when its brace scope closes
            while len(brace_depth) > 1 and brace_depth[-1] <= 0:
                brace_depth.pop()
                if submenu_path:
                    submenu_path.pop()

    return None


def _refresh_grub() -> None:
    """Run update-grub to regenerate grub.cfg after kernel install."""
    try:
        subprocess.run(["update-grub"], capture_output=True, timeout=120)
    except Exception:
        pass


def set_next_boot(kernel_ver: str) -> Tuple[bool, str]:
    """
    Set GRUB to boot kernel_ver on the next boot only (one-time, via grubenv).
    Returns (success, message).
    """
    _refresh_grub()

    entry = _find_grub_entry(kernel_ver)

    # Build fallback entry strings in priority order
    candidates = []
    if entry:
        candidates.append(entry)
    candidates += [
        f"Advanced options for Ubuntu>Ubuntu, with Linux {kernel_ver}",
        f"Ubuntu, with Linux {kernel_ver}",
        kernel_ver,
    ]

    for candidate in candidates:
        r = subprocess.run(
            ["grub-reboot", candidate],
            capture_output=True, text=True,
        )
        if r.returncode == 0:
            return True, f"Next boot set to: {candidate!r}"

    # Final attempt: try to get the numeric index from grub.cfg
    idx = _find_grub_entry_index(kernel_ver)
    if idx is not None:
        r = subprocess.run(
            ["grub-reboot", str(idx)],
            capture_output=True, text=True,
        )
        if r.returncode == 0:
            return True, f"Next boot set to entry index {idx}"

    last_err = r.stderr.strip() if r.returncode != 0 else "unknown error"
    return False, f"grub-reboot failed for {kernel_ver!r}: {last_err}"


def _find_grub_entry_index(kernel_ver: str,
                           grub_cfg: str = "/boot/grub/grub.cfg") -> Optional[int]:
    """Return the 0-based top-level menu index of the kernel entry, if any."""
    if not os.path.exists(grub_cfg):
        return None
    idx = 0
    for line in open(grub_cfg, errors="replace"):
        stripped = line.strip()
        if stripped.startswith("menuentry ") or stripped.startswith("submenu "):
            if kernel_ver in stripped:
                return idx
            idx += 1
    return None


# ── Reboot ────────────────────────────────────────────────────────────────────

def do_reboot(reason: str = "") -> None:
    """Sync filesystems and reboot the machine."""
    if reason:
        print(f"\n  Rebooting: {reason}")
    print("  The analysis will resume automatically via systemd after reboot.")
    print()
    try:
        os.sync()
    except Exception:
        pass
    subprocess.run(["systemctl", "reboot"])
