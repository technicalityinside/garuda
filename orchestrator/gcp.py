import json
import os
import subprocess
import uuid
from typing import Dict, List, Optional

from .base import CloudProvider, VMConfig, VMInstance

_LABEL = 'created-by=benchmark-toolkit'
_DEFAULT_IMAGE_FAMILY = 'ubuntu-2204-lts'
_DEFAULT_IMAGE_PROJECT = 'ubuntu-os-cloud'

# Confidential VM: valid technology choices and their required machine-type prefixes.
# Reference: https://cloud.google.com/confidential-computing/confidential-vm/docs/supported-configurations
_CC_TYPES = {
    'SEV':     ('n2d-', 'c2d-'),
    'SEV_SNP': ('n2d-',),
    'TDX':     ('c3-',),
}
_CC_DEFAULT = 'SEV'


class GCPProvider(CloudProvider):
    """Cloud provider backed by the gcloud CLI."""

    def __init__(self, project: Optional[str] = None):
        self.project = project

    # ── Internal helpers ───────────────────────────────────────────────────────

    def _run(self, args: List[str], check: bool = True) -> object:
        cmd = ['gcloud'] + args + ['--format=json', '--quiet']
        if self.project:
            cmd += [f'--project={self.project}']
        result = subprocess.run(cmd, capture_output=True, text=True)
        if check and result.returncode != 0:
            raise RuntimeError(
                f"gcloud error (exit {result.returncode}):\n{result.stderr.strip()}"
            )
        try:
            return json.loads(result.stdout) if result.stdout.strip() else {}
        except json.JSONDecodeError:
            return result.stdout

    def _zone(self, region: str, zone: Optional[str]) -> str:
        return zone or f'{region}-a'

    def _ssh_keys_meta(self, key_path: str, user: str) -> str:
        pub = os.path.expanduser(key_path) + '.pub'
        if not os.path.exists(pub):
            raise FileNotFoundError(
                f"SSH public key not found: {pub}\n"
                "Generate one with:  ssh-keygen -t rsa -b 4096"
            )
        with open(pub) as f:
            return f'{user}:{f.read().strip()}'

    def _parse_instance(self, item: dict, zone: str, ssh_user: str, ssh_key_path: str) -> VMInstance:
        ni = item.get('networkInterfaces', [{}])[0]
        public_ip = ni.get('accessConfigs', [{}])[0].get('natIP', '')
        region = zone.rsplit('-', 1)[0]
        return VMInstance(
            vm_id=item['name'],
            name=item['name'],
            public_ip=public_ip,
            private_ip=ni.get('networkIP', ''),
            state=item.get('status', ''),
            provider='gcp',
            region=region,
            instance_type=item.get('machineType', '').split('/')[-1],
            ssh_user=ssh_user,
            ssh_key_path=ssh_key_path,
            extra={'zone': zone},
        )

    # ── CloudProvider interface ────────────────────────────────────────────────

    def _confidential_args(self, config: VMConfig) -> List[str]:
        """Return gcloud flags required for Confidential VM launch."""
        if not config.confidential_compute:
            return []
        ctype = (config.confidential_type or _CC_DEFAULT).upper()
        if ctype not in _CC_TYPES:
            raise ValueError(
                f"Unknown GCP confidential type: {ctype!r}. "
                f"Choose from: {', '.join(_CC_TYPES)}"
            )
        required_prefixes = _CC_TYPES[ctype]
        if not any(config.instance_type.startswith(p) for p in required_prefixes):
            print(
                f"[gcp] WARNING: Confidential type '{ctype}' requires a machine type "
                f"starting with one of {required_prefixes}. "
                f"Got '{config.instance_type}'. The API will reject this if incompatible."
            )
        print(f"[gcp] Confidential Computing enabled: type={ctype}")
        return [
            f'--confidential-compute-type={ctype}',
            '--maintenance-policy=TERMINATE',  # live migration not supported for CVM
        ]

    def create_vm(self, config: VMConfig) -> VMInstance:
        name = config.vm_name or f'benchmark-{uuid.uuid4().hex[:8]}'
        zone = self._zone(config.region, config.zone)
        key_path = os.path.expanduser(config.ssh_key_path or '~/.ssh/id_rsa')
        ssh_meta = self._ssh_keys_meta(key_path, config.ssh_user)

        image_args = (
            [f'--image={config.image}']
            if config.image
            else [
                f'--image-family={_DEFAULT_IMAGE_FAMILY}',
                f'--image-project={_DEFAULT_IMAGE_PROJECT}',
            ]
        )

        cc_args = self._confidential_args(config)
        print(f"[gcp] Creating instance '{name}' in {zone} ({config.instance_type})...")
        data = self._run([
            'compute', 'instances', 'create', name,
            f'--zone={zone}',
            f'--machine-type={config.instance_type}',
            f'--boot-disk-size={config.disk_size_gb}GB',
            f'--metadata=ssh-keys={ssh_meta}',
            f'--labels={_LABEL}',
        ] + image_args + cc_args)

        item = data[0] if isinstance(data, list) else data
        return self._parse_instance(item, zone, config.ssh_user, key_path)

    def get_vm(self, vm_id: str, extra: Dict = None) -> VMInstance:
        extra = extra or {}
        zone = extra.get('zone', 'us-central1-a')
        data = self._run(['compute', 'instances', 'describe', vm_id, f'--zone={zone}'])
        return self._parse_instance(
            data, zone,
            ssh_user=extra.get('ssh_user', 'ubuntu'),
            ssh_key_path=extra.get('ssh_key_path', '~/.ssh/id_rsa'),
        )

    def delete_vm(self, instance: VMInstance) -> None:
        zone = instance.extra.get('zone', 'us-central1-a')
        print(f"[gcp] Deleting instance '{instance.name}' in {zone}...")
        self._run(['compute', 'instances', 'delete', instance.vm_id, f'--zone={zone}'])

    def list_vms(self) -> List[VMInstance]:
        data = self._run(
            ['compute', 'instances', 'list',
             '--filter=labels.created-by=benchmark-toolkit'],
            check=False,
        )
        if not isinstance(data, list):
            return []
        result = []
        for item in data:
            zone = item.get('zone', '').split('/')[-1]
            result.append(self._parse_instance(item, zone, 'ubuntu', '~/.ssh/id_rsa'))
        return result
