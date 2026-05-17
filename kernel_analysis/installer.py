"""Kernel installation, GRUB manipulation, and reboot helpers."""

import glob
import gzip
import os
import re
import shutil
import subprocess
import sys
import tarfile
import urllib.request
from typing import List, Optional, Tuple

_DEFAULT_BUILD_BASE = "/var/cache/garuda/kernel-builds"

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


def parse_kernel_specs(specs_str: str) -> List[dict]:
    """
    Parse a comma-separated string of kernel specs into a list of dicts.

    Formats:
      6.8.0-55-generic                        — apt install (plain version)
      apt:6.8.0-55-generic                    — apt install (explicit)
      github:owner/repo:branch[@label]        — build from GitHub clone
      github:https://github.com/o/r:branch[@label]
      tarball:https://example.com/k.tar.gz[@label]  — build from tarball URL

    Each entry returned:  {"version": str, "source_spec": dict}
    """
    results = []
    for raw in specs_str.split(","):
        raw = raw.strip()
        if not raw:
            continue

        if raw.startswith("github:"):
            rest = raw[len("github:"):]
            label = None
            if "@" in rest:
                rest, label = rest.rsplit("@", 1)
                label = label.strip()
            colon_idx = rest.rfind(":")
            if colon_idx == -1:
                raise ValueError(
                    f"github: spec missing branch — use github:owner/repo:branch: {raw!r}"
                )
            repo_part = rest[:colon_idx].strip()
            branch = rest[colon_idx + 1:].strip()
            if not repo_part.startswith("http"):
                repo_part = f"https://github.com/{repo_part}"
            version = label or f"{repo_part.rstrip('/').split('/')[-1]}-{branch}"
            results.append({
                "version": version,
                "source_spec": {"type": "github", "repo": repo_part,
                                "branch": branch, "label": label},
            })

        elif raw.startswith("tarball:"):
            rest = raw[len("tarball:"):]
            label = None
            if "@" in rest:
                rest, label = rest.rsplit("@", 1)
                label = label.strip()
            url = rest.strip()
            if not label:
                basename = url.rstrip("/").split("/")[-1]
                for ext in (".tar.gz", ".tar.bz2", ".tar.xz", ".tgz"):
                    if basename.endswith(ext):
                        basename = basename[: -len(ext)]
                        break
                label = basename
            results.append({
                "version": label,
                "source_spec": {"type": "tarball", "url": url, "label": label},
            })

        elif raw.startswith("apt:"):
            ver = raw[len("apt:"):].strip()
            results.append({"version": ver, "source_spec": {"type": "apt"}})

        else:
            results.append({"version": raw, "source_spec": {"type": "apt"}})

    return results


def _configure_kernel(src_dir: str) -> Tuple[bool, str]:
    """Copy the running kernel's .config into src_dir and run make olddefconfig."""
    running = current_kernel()
    config_src = f"/boot/config-{running}"

    dest = os.path.join(src_dir, ".config")
    if os.path.exists(config_src):
        shutil.copy(config_src, dest)
    elif os.path.exists("/proc/config.gz"):
        with gzip.open("/proc/config.gz", "rb") as gz_f, open(dest, "wb") as out_f:
            out_f.write(gz_f.read())
    else:
        return False, f"No kernel config found at {config_src} or /proc/config.gz"

    r = subprocess.run(["make", "olddefconfig"], cwd=src_dir, timeout=300)
    if r.returncode != 0:
        return False, "make olddefconfig failed"

    # Clear distro-specific cert paths that don't exist in mainline trees.
    # Ubuntu sets these to "debian/canonical-certs.pem" etc.; keeping them
    # causes "No rule to make target" errors when building mainline source.
    for key in ("SYSTEM_TRUSTED_KEYS", "SYSTEM_REVOCATION_KEYS"):
        subprocess.run(
            ["scripts/config", "--set-str", key, ""],
            cwd=src_dir, timeout=30,
        )

    return True, "Kernel config prepared"


_BUILD_DEPS = (
    "build-essential", "bc", "bison", "flex",
    "libssl-dev", "libelf-dev", "libdw-dev", "dwarves",
    "debhelper", "rsync", "libncurses-dev", "gawk",
)


def _ensure_build_deps() -> Tuple[bool, str]:
    """Install kernel build dependencies if any are missing."""
    env = {**os.environ, "DEBIAN_FRONTEND": "noninteractive"}
    # Check which packages are actually missing before installing
    missing = []
    for pkg in _BUILD_DEPS:
        r = subprocess.run(
            ["dpkg-query", "-W", "-f=${Status}", pkg],
            capture_output=True, text=True,
        )
        if "install ok installed" not in r.stdout:
            missing.append(pkg)

    if not missing:
        return True, "Build dependencies already satisfied"

    print(f"    Installing missing build deps: {' '.join(missing)} ...")
    r = subprocess.run(
        ["apt-get", "install", "-y", "--no-install-recommends"] + missing,
        env=env,
        timeout=300,
    )
    if r.returncode != 0:
        return False, f"Failed to install build dependencies: {' '.join(missing)}"
    return True, f"Installed: {' '.join(missing)}"


def _build_and_install(src_dir: str) -> Tuple[bool, str, str]:
    """
    Build kernel .deb packages via make bindeb-pkg and install them.
    Returns (ok, message, kernel_release_string).
    """
    ok, msg = _ensure_build_deps()
    if not ok:
        return False, msg, ""

    nproc = os.cpu_count() or 1
    log_path = os.path.join(os.path.dirname(src_dir), "build.log")
    print(f"    make -j{nproc} bindeb-pkg  (this may take 30–90 min) ...", flush=True)
    print(f"    Full build log: {log_path}", flush=True)

    returncode = None
    try:
        with open(log_path, "wb") as log_f:
            proc = subprocess.Popen(
                ["make", f"-j{nproc}", "bindeb-pkg", "LOCALVERSION="],
                cwd=src_dir,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
            for chunk in iter(lambda: proc.stdout.read(4096), b""):
                sys.stdout.buffer.write(chunk)
                sys.stdout.buffer.flush()
                log_f.write(chunk)
            proc.wait(timeout=7200)
            returncode = proc.returncode
    except subprocess.TimeoutExpired:
        proc.kill()
        return False, "make bindeb-pkg timed out after 2 hours", ""

    if returncode != 0:
        # Extract the actual compiler/linker error lines and show them
        try:
            with open(log_path) as f:
                lines = f.readlines()
            error_lines = [
                l.rstrip() for l in lines
                if re.search(r'\berror:', l, re.IGNORECASE)
                and not re.search(r'\bwarning:.*error:', l, re.IGNORECASE)
            ]
            if error_lines:
                print(f"\n    ── Build errors ({len(error_lines)} found, last 15) ──")
                for l in error_lines[-15:]:
                    print(f"    {l}")
                print(f"    ── Full log: {log_path} ──\n")
        except Exception:
            pass
        return False, f"make bindeb-pkg failed — see {log_path}", ""

    # .deb files land in the parent of the source tree
    parent = os.path.dirname(src_dir)
    debs = sorted(glob.glob(os.path.join(parent, "linux-image-*.deb")))
    if not debs:
        debs = sorted(glob.glob(os.path.join(src_dir, "linux-image-*.deb")))
    if not debs:
        return False, "No linux-image-*.deb found after build", ""

    # Derive actual kernel release version
    kernel_release = ""
    kr_file = os.path.join(src_dir, "include/config/kernel.release")
    if os.path.exists(kr_file):
        kernel_release = open(kr_file).read().strip()
    if not kernel_release:
        m = re.search(r"linux-image-([^_]+)_", os.path.basename(debs[0]))
        if m:
            kernel_release = m.group(1)

    print(f"    dpkg -i {len(debs)} package(s) ...")
    r = subprocess.run(["dpkg", "-i"] + debs, timeout=300)
    if r.returncode != 0:
        return False, f"dpkg -i failed (exit {r.returncode})", ""

    return True, f"Installed kernel {kernel_release}", kernel_release


def install_from_github(source_spec: dict,
                        build_base: str = _DEFAULT_BUILD_BASE) -> Tuple[bool, str, str]:
    """Clone a GitHub repo at a branch/tag and build+install the kernel."""
    repo = source_spec["repo"]
    branch = source_spec["branch"]
    label = source_spec.get("label") or branch

    # When a @label (e.g. @v7.0) is specified, use it as the git ref so that
    # tags are checked out rather than just treated as display names.
    ref = label if label != branch else branch

    clone_dir = os.path.join(build_base, f"linux-github-{re.sub(r'[^A-Za-z0-9._-]', '_', label)}")
    os.makedirs(build_base, exist_ok=True)

    if os.path.isdir(clone_dir):
        print(f"    Reusing existing clone at {clone_dir}")
    else:
        print(f"    git clone --depth=1 -b {ref} {repo} ...")
        r = subprocess.run(
            ["git", "clone", "--depth=1", "-b", ref, repo, clone_dir],
            timeout=1800,
        )
        if r.returncode != 0:
            return False, f"git clone failed for {repo}@{ref}", ""

    ok, msg = _configure_kernel(clone_dir)
    if not ok:
        return False, msg, ""
    return _build_and_install(clone_dir)


def install_from_tarball(source_spec: dict,
                         build_base: str = _DEFAULT_BUILD_BASE) -> Tuple[bool, str, str]:
    """Download a kernel tarball, extract it, and build+install."""
    url = source_spec["url"]
    label = source_spec.get("label") or "kernel"

    os.makedirs(build_base, exist_ok=True)
    basename = url.rstrip("/").split("/")[-1]
    local_tar = os.path.join(build_base, basename)
    src_dir = os.path.join(
        build_base, f"linux-tarball-{re.sub(r'[^A-Za-z0-9._-]', '_', label)}"
    )

    if not os.path.exists(local_tar):
        print(f"    Downloading {url} ...")
        try:
            urllib.request.urlretrieve(url, local_tar)
        except Exception as e:
            return False, f"Download failed: {e}", ""
    else:
        print(f"    Using cached tarball {local_tar}")

    if os.path.isdir(src_dir):
        print(f"    Reusing existing source at {src_dir}")
    else:
        print(f"    Extracting {basename} ...")
        try:
            with tarfile.open(local_tar) as tf:
                # Find the single top-level directory in the archive
                top_dirs = {m.name.split("/")[0] for m in tf.getmembers()
                            if "/" in m.name}
                tf.extractall(build_base)
            if top_dirs:
                extracted = os.path.join(build_base, sorted(top_dirs)[0])
                os.rename(extracted, src_dir)
        except Exception as e:
            return False, f"Extraction failed: {e}", ""

    ok, msg = _configure_kernel(src_dir)
    if not ok:
        return False, msg, ""
    return _build_and_install(src_dir)


def install_kernel(entry,
                   build_base: str = _DEFAULT_BUILD_BASE) -> Tuple[bool, str, str]:
    """
    Install a kernel based on entry.source_spec.
    Returns (ok, message, actual_kernel_version_string).

    For apt builds the actual version equals entry.version.
    For github/tarball the actual version is the built kernelrelease string.
    """
    source_spec = getattr(entry, "source_spec", None) or {"type": "apt"}
    spec_type = source_spec.get("type", "apt")

    if spec_type == "apt":
        if is_installed(entry.version):
            return True, f"Already installed: {entry.version}", entry.version
        ok, msg = install(entry.version)
        return ok, msg, entry.version if ok else ""

    elif spec_type == "github":
        return install_from_github(source_spec, build_base)

    elif spec_type == "tarball":
        return install_from_tarball(source_spec, build_base)

    else:
        return False, f"Unknown source type: {spec_type!r}", ""


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
