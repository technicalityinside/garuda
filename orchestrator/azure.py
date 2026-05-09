import json
import os
import subprocess
import uuid
from typing import Dict, List, Optional

from .base import CloudProvider, VMConfig, VMInstance

_TAG = 'created-by=benchmark-toolkit'

# Confidential VM: disk encryption types supported by Azure CVM.
# Reference: https://learn.microsoft.com/en-us/azure/confidential-computing/confidential-vm-overview
_CC_ENCRYPTION_TYPES = ('VMGuestStateOnly', 'DiskWithVMGuestState')
_CC_DEFAULT_ENCRYPTION = 'VMGuestStateOnly'
# Ubuntu image with AMD SEV-SNP CVM support (GA on Azure).
_CC_DEFAULT_IMAGE = 'Canonical:ubuntu-24_04-lts:cvm:latest'
# VM size families that support Confidential VMs on Azure.
# Azure sizes follow the pattern Standard_DC*as_v5 / Standard_EC*as_v5.
_CC_SIZE_HINTS = ('Standard_DC', 'Standard_EC')


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

    def _confidential_args(self, config: VMConfig) -> tuple:
        """Return (extra_cli_args, image_override) for Confidential VM launch."""
        if not config.confidential_compute:
            return [], None
        enc = config.confidential_type or _CC_DEFAULT_ENCRYPTION
        if enc not in _CC_ENCRYPTION_TYPES:
            raise ValueError(
                f"Unknown Azure confidential encryption type: {enc!r}. "
                f"Choose from: {', '.join(_CC_ENCRYPTION_TYPES)}"
            )
        if not any(config.instance_type.startswith(h) for h in _CC_SIZE_HINTS):
            print(
                f"[azure] WARNING: Confidential VMs require a DCasv5/ECasv5-family size. "
                f"Got '{config.instance_type}'. The API will reject this if incompatible.\n"
                f"  Recommended sizes: Standard_DC4as_v5, Standard_EC4as_v5, etc."
            )
        print(f"[azure] Confidential Computing enabled: encryption={enc}")
        args = [
            '--security-type', 'ConfidentialVM',
            '--os-disk-security-encryption-type', enc,
            '--enable-secure-boot', 'true',
            '--enable-vtpm', 'true',
        ]
        image_override = None if config.image else _CC_DEFAULT_IMAGE
        return args, image_override

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

        cc_args, cc_image = self._confidential_args(config)
        image = config.image or cc_image or 'Ubuntu2204'
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
        ] + cc_args)

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
