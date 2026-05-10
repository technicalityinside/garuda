"""Systemd service management for auto-resume after reboot."""

import os
import subprocess
import sys
from typing import Tuple

SERVICE_NAME = "garuda-kernel-analysis"
SERVICE_FILE = f"/etc/systemd/system/{SERVICE_NAME}.service"


def install_service(main_py: str, state_file: str) -> Tuple[bool, str]:
    """
    Write and enable a oneshot systemd service that resumes the analysis
    after each reboot by calling:  python3 <main_py> kernel-analyze --resume
    """
    python = sys.executable
    unit = f"""\
[Unit]
Description=Garuda Kernel Analysis — auto-resume after reboot
After=network.target
# Only run if a state file exists (guards against stale service after abort)
ConditionPathExists={state_file}

[Service]
Type=oneshot
ExecStart={python} {main_py} kernel-analyze --resume --state-file {state_file}
StandardOutput=journal+console
StandardError=journal+console
User=root
# Give the service up to 3 h to complete one kernel's benchmarks
TimeoutStartSec=10800
RemainAfterExit=no

[Install]
WantedBy=multi-user.target
"""
    try:
        with open(SERVICE_FILE, "w") as f:
            f.write(unit)
        subprocess.run(["systemctl", "daemon-reload"], check=True, capture_output=True)
        subprocess.run(["systemctl", "enable", SERVICE_NAME], check=True, capture_output=True)
        return True, f"Enabled {SERVICE_NAME}.service"
    except Exception as exc:
        return False, str(exc)


def remove_service() -> None:
    """Disable and delete the resume service (called when analysis completes or aborts)."""
    try:
        subprocess.run(
            ["systemctl", "disable", "--now", SERVICE_NAME],
            capture_output=True, timeout=15,
        )
    except Exception:
        pass
    try:
        if os.path.exists(SERVICE_FILE):
            os.remove(SERVICE_FILE)
        subprocess.run(["systemctl", "daemon-reload"], capture_output=True, timeout=15)
    except Exception:
        pass


def service_status() -> str:
    """Return the active state of the service (active, inactive, failed, not-found)."""
    try:
        r = subprocess.run(
            ["systemctl", "is-active", SERVICE_NAME],
            capture_output=True, text=True, timeout=5,
        )
        return r.stdout.strip() or "unknown"
    except Exception:
        return "unknown"
