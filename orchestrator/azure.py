import json
import os
import subprocess
import uuid
from typing import Dict, List, Optional

from .base import CloudProvider, VMConfig, VMInstance

_TAG = 'created-by=benchmark-toolkit'


class AzureProvider(CloudProvider):
    """Cloud provider backed by the az CLI."""

    def __init__(self, subscription: Optional[str] = None):
        self.subscription = subscription

    # ── Internal helpers ───────────────────────────────────────────────────────

    def _run(self, args: List[str], check: bool = True) -> object:
        cmd = ['az'] + args + ['--output', 'json']
        if self.subscription:
            cmd += ['--subscription', self.subscription]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if check and result.returncode != 0:
            raise RuntimeError(
                f"az error (exit {result.returncode}):\n{result.stderr.strip()}"
            )
        try:
            return json.loads(result.stdout) if result.stdout.strip() else {}
        except json.JSONDecodeError:
            return result.stdout

    def _resource_group(self, config: VMConfig) -> str:
        return config.resource_group or f'benchmark-rg-{config.region}'

    def _ensure_resource_group(self, rg: str, location: str) -> None:
        print(f"[azure] Ensuring resource group '{rg}' in {location}...")
        self._run(['group', 'create', '--name', rg, '--location', location, '--tags', _TAG])

    # ── CloudProvider interface ────────────────────────────────────────────────

    def create_vm(self, config: VMConfig) -> VMInstance:
        name = config.vm_name or f'benchmark-{uuid.uuid4().hex[:8]}'
        rg = self._resource_group(config)
        key_path = os.path.expanduser(config.ssh_key_path or '~/.ssh/id_rsa')
        pub_key = key_path + '.pub'
        if not os.path.exists(pub_key):
            raise FileNotFoundError(
                f"SSH public key not found: {pub_key}\n"
                "Generate one with:  ssh-keygen -t rsa -b 4096"
            )

        self._ensure_resource_group(rg, config.region)

        image = config.image or 'Ubuntu2204'
        print(f"[azure] Creating VM '{name}' ({config.instance_type}) in {config.region}...")
        data = self._run([
            'vm', 'create',
            '--resource-group', rg,
            '--name', name,
            '--image', image,
            '--size', config.instance_type,
            '--admin-username', config.ssh_user,
            '--ssh-key-values', pub_key,
            '--location', config.region,
            '--os-disk-size-gb', str(config.disk_size_gb),
            '--tags', _TAG,
        ])

        return VMInstance(
            vm_id=name,
            name=name,
            public_ip=data.get('publicIpAddress', ''),
            private_ip=data.get('privateIpAddress', ''),
            state='running',          # az vm create blocks until ready
            provider='azure',
            region=config.region,
            instance_type=config.instance_type,
            ssh_user=config.ssh_user,
            ssh_key_path=key_path,
            extra={'resource_group': rg},
        )

    def get_vm(self, vm_id: str, extra: Dict = None) -> VMInstance:
        extra = extra or {}
        rg = extra.get('resource_group', 'benchmark-rg')
        data = self._run(['vm', 'show',
                          '--name', vm_id,
                          '--resource-group', rg,
                          '--show-details'])
        return VMInstance(
            vm_id=vm_id,
            name=data.get('name', vm_id),
            public_ip=data.get('publicIps', ''),
            private_ip=data.get('privateIps', ''),
            state=data.get('powerState', ''),
            provider='azure',
            region=data.get('location', extra.get('region', '')),
            instance_type=data.get('hardwareProfile', {}).get('vmSize', ''),
            ssh_user=extra.get('ssh_user', 'ubuntu'),
            ssh_key_path=extra.get('ssh_key_path', '~/.ssh/id_rsa'),
            extra=extra,
        )

    def delete_vm(self, instance: VMInstance) -> None:
        rg = instance.extra.get('resource_group', 'benchmark-rg')
        print(f"[azure] Deleting resource group '{rg}' (contains VM '{instance.name}')...")
        # Deleting the resource group removes the VM and all attached resources.
        self._run(['group', 'delete', '--name', rg, '--yes', '--no-wait'])

    def list_vms(self) -> List[VMInstance]:
        data = self._run(
            ['vm', 'list', '--show-details',
             '--query', "[?tags.\"created-by\"=='benchmark-toolkit']"],
            check=False,
        )
        if not isinstance(data, list):
            return []
        result = []
        for item in data:
            rg = item.get('resourceGroup', '')
            result.append(VMInstance(
                vm_id=item['name'],
                name=item['name'],
                public_ip=item.get('publicIps', ''),
                private_ip=item.get('privateIps', ''),
                state=item.get('powerState', ''),
                provider='azure',
                region=item.get('location', ''),
                instance_type=item.get('hardwareProfile', {}).get('vmSize', ''),
                ssh_user='ubuntu',
                ssh_key_path='~/.ssh/id_rsa',
                extra={'resource_group': rg},
            ))
        return result
