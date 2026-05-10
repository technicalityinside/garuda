import json
import os
import sys
import time
from typing import Callable, Dict, List, Optional

from .base import CloudProvider, VMConfig, VMInstance
from .remote import RemoteExecutor

REMOTE_DIR = '~/benchmark_toolkit'
REMOTE_STATE_FILE = '/var/lib/garuda/kernel_analysis.json'


class CloudBenchmarkRunner:
    """Orchestrates the full lifecycle: provision VM → deploy toolkit → run → fetch results."""

    def __init__(self, provider: CloudProvider):
        self.provider = provider

    def _remote(self, instance: VMInstance) -> RemoteExecutor:
        return RemoteExecutor(
            host=instance.public_ip,
            user=instance.ssh_user,
            key_path=instance.ssh_key_path,
        )

    # ── Lifecycle steps ────────────────────────────────────────────────────────

    def provision(self, config: VMConfig, verbose: bool = False) -> VMInstance:
        """Create VM, wait for running state, wait for SSH."""
        instance = self.provider.create_vm(config)
        print(f"[cloud] VM '{instance.name}' created (id={instance.vm_id}). "
              "Waiting for running state...")
        instance = self.provider.wait_for_ready(instance, timeout=300)
        print(f"[cloud] VM running at {instance.public_ip}. Waiting for SSH...")
        if not self.provider.wait_for_ssh(instance, timeout=300):
            raise RuntimeError(
                f"SSH did not become available on {instance.public_ip} within 5 minutes.\n"
                "Check your security group / firewall rules."
            )
        print(f"[cloud] SSH ready.")
        return instance

    def setup_toolkit(self, instance: VMInstance, local_dir: str,
                      verbose: bool = False) -> None:
        """Install Python + rsync, then sync the toolkit to the VM."""
        remote = self._remote(instance)

        print("[cloud] Installing system dependencies (python3, pip3, rsync)...")
        stdout, stderr, rc = remote.run(
            'sudo apt-get update -qq && '
            'sudo DEBIAN_FRONTEND=noninteractive apt-get install -y '
            'python3 python3-pip rsync 2>&1',
            timeout=300,
        )
        if rc != 0:
            raise RuntimeError(f"Dependency install failed:\n{stdout}\n{stderr}")
        if verbose:
            print(stdout)

        print(f"[cloud] Syncing toolkit to remote:{REMOTE_DIR} ...")
        remote.run(f'mkdir -p {REMOTE_DIR}', timeout=15)
        remote.copy_to(local_dir, REMOTE_DIR)

        # Install optional Python packages; non-fatal
        remote.run(
            f'cd {REMOTE_DIR} && pip3 install -r requirements.txt -q 2>&1',
            timeout=120,
        )
        print("[cloud] Toolkit deployed.")

    def run_setup(self, instance: VMInstance, workload: str,
                  verbose: bool = False) -> None:
        """Run 'setup' on the remote VM to install workload binaries."""
        remote = self._remote(instance)
        print(f"[cloud] Setting up workload '{workload}' on remote VM...")
        stdout, stderr, rc = remote.run(
            f'cd {REMOTE_DIR} && python3 main.py setup --workload {workload} 2>&1',
            timeout=600,
        )
        if verbose:
            print(stdout)
        if rc != 0:
            raise RuntimeError(
                f"Workload setup for '{workload}' failed (exit {rc}):\n{stdout}"
            )
        print(f"[cloud] Workload '{workload}' ready.")

    def run_benchmark(self, instance: VMInstance, bench_cmd: List[str],
                      verbose: bool = False) -> int:
        """Run a benchmark command string on the remote VM."""
        remote = self._remote(instance)
        cmd_str = ' '.join(bench_cmd)
        print(f"[cloud] Running: python3 main.py {cmd_str}")
        stdout, stderr, rc = remote.run(
            f'cd {REMOTE_DIR} && python3 main.py {cmd_str} 2>&1',
            timeout=7200,
        )
        print(stdout)
        if rc != 0:
            print(f"[cloud] Benchmark exited with code {rc}.", file=sys.stderr)
        return rc

    def fetch_results(self, instance: VMInstance, local_results_dir: str) -> None:
        """Download the results/ directory from the remote VM."""
        remote = self._remote(instance)
        print(f"[cloud] Fetching results → {local_results_dir} ...")
        remote.copy_from(f'{REMOTE_DIR}/results', local_results_dir)
        print("[cloud] Results downloaded.")

    def destroy(self, instance: VMInstance) -> None:
        """Delete the VM."""
        self.provider.delete_vm(instance)
        print(f"[cloud] VM '{instance.name}' destroyed.")

    # ── Kernel analysis helpers ────────────────────────────────────────────────

    def install_kernels_remote(
        self,
        instance: VMInstance,
        kernels: List[str],
        verbose: bool = False,
    ) -> None:
        """
        Pre-install all kernel versions on the remote VM via apt before the
        analysis loop starts, so reboots between kernels are not delayed by
        package downloads.
        """
        remote = self._remote(instance)
        env_prefix = "DEBIAN_FRONTEND=noninteractive"

        for kver in kernels:
            image_pkg = f"linux-image-{kver}"
            headers_pkg = f"linux-headers-{kver}"

            # Check if already installed
            _, _, rc = remote.run(
                f"test -f /boot/vmlinuz-{kver}", timeout=10
            )
            if rc == 0:
                print(f"[cloud] Kernel {kver} already installed.")
                continue

            print(f"[cloud] Installing kernel {kver} ...")
            stdout, stderr, rc = remote.run(
                f"sudo {env_prefix} apt-get install -y --no-install-recommends "
                f"{image_pkg} {headers_pkg} 2>&1 || "
                f"sudo {env_prefix} apt-get install -y --no-install-recommends "
                f"{image_pkg} 2>&1",
                timeout=300,
            )
            if verbose:
                print(stdout)
            if rc != 0 and "Unable to locate package" not in stdout:
                raise RuntimeError(
                    f"Failed to install kernel {kver}:\n{stdout}\n{stderr}"
                )
            if rc == 0:
                print(f"[cloud]   Installed {kver}.")
            else:
                print(f"[cloud]   [WARN] Could not install {kver} — will try during analysis.")

    def _poll_kernel_analysis(
        self,
        instance: VMInstance,
        total_kernels: int,
        verbose: bool = False,
        poll_interval: int = 30,
        max_no_response: int = 40,
        max_total_hours: float = 12.0,
    ) -> str:
        """
        Poll the remote state file via SSH until the analysis reaches a terminal
        state (done / failed).  Handles VM reboots transparently by retrying SSH
        after connection failures.

        Returns the final session status string.
        """
        remote = self._remote(instance)
        state_check = (
            "python3 -c \""
            f"import json; d=json.load(open('{REMOTE_STATE_FILE}')); "
            "done=[k for k in d['kernels'] if k['status'] in ('pushed','failed','skipped')]; "
            "print(d['status'], len(done), len(d['kernels']), "
            "[k['version']+'='+k['status'] for k in d['kernels']])\""
        )

        deadline = time.time() + max_total_hours * 3600
        no_response = 0
        last_done = -1

        print(
            f"[cloud] Monitoring kernel analysis ({total_kernels} kernel(s)). "
            "VM will reboot between kernels — reconnecting automatically."
        )

        while time.time() < deadline:
            time.sleep(poll_interval)

            try:
                stdout, _, rc = remote.run(state_check, timeout=20)
            except Exception:
                rc = 1
                stdout = ""

            if rc == 0 and stdout.strip():
                no_response = 0
                parts = stdout.strip().split(None, 3)
                status = parts[0] if parts else "unknown"
                done_count = int(parts[1]) if len(parts) > 1 else 0
                total_count = int(parts[2]) if len(parts) > 2 else total_kernels
                detail = parts[3] if len(parts) > 3 else ""

                if done_count != last_done:
                    print(
                        f"[cloud] Progress: {done_count}/{total_count} kernels done  "
                        f"(session={status})"
                    )
                    if verbose and detail:
                        print(f"         {detail}")
                    last_done = done_count

                if status in ("done", "failed"):
                    print(f"[cloud] Analysis complete: {status}")
                    return status
            else:
                no_response += 1
                if no_response % 4 == 1:
                    print(
                        f"[cloud] VM unreachable (reboot in progress?) "
                        f"— retrying ({no_response}/{max_no_response})"
                    )
                if no_response >= max_no_response:
                    raise RuntimeError(
                        f"VM at {instance.public_ip} has been unreachable for "
                        f"~{no_response * poll_interval // 60} minutes. "
                        "Check the VM directly or increase --poll-timeout."
                    )

        raise RuntimeError(
            f"Kernel analysis exceeded the {max_total_hours:.0f}-hour timeout."
        )

    def run_kernel_analysis(
        self,
        instance: VMInstance,
        kernels: List[str],
        workloads: List[str],
        config: Optional[str],
        iterations: int,
        push_url: Optional[str],
        api_key: str,
        kernel_config: str,
        local_results_dir: str,
        verbose: bool = False,
        poll_interval: int = 30,
        max_no_response: int = 40,
    ) -> str:
        """
        Full kernel analysis pipeline on a provisioned VM:

          1. Pre-install all kernels via apt (avoids download stalls mid-reboot)
          2. Set up each workload binary
          3. Launch `kernel-analyze` on the VM (installs systemd service,
             triggers first grub-reboot, reboots the VM)
          4. Poll SSH until the analysis state file shows "done" or "failed",
             transparently reconnecting after each reboot
          5. Fetch results/ from the VM

        Returns the final session status ("done" or "failed").
        """
        remote = self._remote(instance)

        # Step 1 – pre-install all kernels
        self.install_kernels_remote(instance, kernels, verbose)

        # Step 2 – set up workload binaries
        for wl in workloads:
            self.run_setup(instance, wl, verbose)

        # Step 3 – build and launch kernel-analyze
        kernels_arg = ",".join(kernels)
        workloads_arg = ",".join(workloads)
        ka_args = [
            f"--kernels {kernels_arg}",
            f"--workloads {workloads_arg}",
            f"--iterations {iterations}",
            f"--kernel-config {kernel_config}",
            f"--system-name {instance.name}",
        ]
        if config:
            ka_args.append(f"--config {config}")
        if push_url:
            ka_args.append(f"--push-url {push_url}")
        if api_key:
            ka_args.append(f"--api-key {api_key}")

        ka_cmd = "sudo python3 main.py kernel-analyze " + " ".join(ka_args)
        print(f"[cloud] Starting kernel analysis on VM ...")
        if verbose:
            print(f"[cloud]   {ka_cmd}")

        # The VM will reboot mid-command — SSH disconnect is expected and not an error.
        try:
            stdout, stderr, rc = remote.run(
                f"cd {REMOTE_DIR} && {ka_cmd} 2>&1",
                timeout=180,  # enough for grub-reboot setup + reboot initiation
            )
            if verbose and stdout:
                print(stdout)
            # rc 130 = KeyboardInterrupt; non-zero is expected if VM rebooted
        except (TimeoutError, Exception) as exc:
            if verbose:
                print(f"[cloud] SSH disconnected (expected — VM is rebooting): {exc}")

        # Brief wait for the VM to start rebooting before we begin polling
        print("[cloud] Waiting for VM to reboot into first kernel ...")
        time.sleep(45)

        # Step 4 – poll until done
        status = self._poll_kernel_analysis(
            instance,
            total_kernels=len(kernels),
            verbose=verbose,
            poll_interval=poll_interval,
            max_no_response=max_no_response,
        )

        # Step 5 – fetch results
        self.fetch_results(instance, local_results_dir)

        # Also retrieve the state file for score / summary (SSH cat → local file)
        try:
            stdout, _, rc = remote.run(
                "cat /var/lib/garuda/kernel_analysis.json", timeout=15
            )
            if rc == 0 and stdout.strip():
                dest = os.path.join(local_results_dir, "kernel_analysis.json")
                os.makedirs(local_results_dir, exist_ok=True)
                with open(dest, "w") as f:
                    f.write(stdout)
        except Exception:
            pass

        return status

    # ── High-level entry point ─────────────────────────────────────────────────

    def full_run(
        self,
        vm_config: VMConfig,
        workload: str,
        bench_cmd: List[str],
        local_dir: str,
        results_dir: str,
        teardown: bool = True,
        verbose: bool = False,
        on_provisioned: Optional[Callable[[VMInstance], None]] = None,
    ) -> int:
        """
        Complete end-to-end flow:
          provision → deploy toolkit → workload setup → benchmark → fetch results → (destroy)

        on_provisioned: optional callback called immediately after the VM is ready,
                        useful for persisting VM info before any benchmark work starts.
        """
        instance = None
        rc = 0
        try:
            instance = self.provision(vm_config, verbose)
            if on_provisioned:
                on_provisioned(instance)
            self.setup_toolkit(instance, local_dir, verbose)
            self.run_setup(instance, workload, verbose)
            rc = self.run_benchmark(instance, bench_cmd, verbose)
            self.fetch_results(instance, results_dir)
        except Exception:
            raise
        finally:
            if instance:
                if teardown:
                    try:
                        self.destroy(instance)
                    except Exception as exc:
                        print(f"[cloud] Warning: VM cleanup failed: {exc}", file=sys.stderr)
                else:
                    key = instance.ssh_key_path
                    user = instance.ssh_user
                    ip = instance.public_ip
                    print(f"\n[cloud] VM kept running:")
                    print(f"  Name:     {instance.name}")
                    print(f"  IP:       {ip}")
                    print(f"  SSH:      ssh -i {key} {user}@{ip}")
                    print(f"  Run more: python3 main.py cloud-exec --vm-name {instance.name} --workload ...")
                    print(f"  Destroy:  python3 main.py cloud-destroy --vm-name {instance.name}")
        return rc
