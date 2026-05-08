import sys
from typing import Callable, List, Optional

from .base import CloudProvider, VMConfig, VMInstance
from .remote import RemoteExecutor

REMOTE_DIR = '~/benchmark_toolkit'


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
