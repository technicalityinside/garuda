import json
import os
import subprocess
import uuid
from typing import Dict, List, Optional

from .base import CloudProvider, VMConfig, VMInstance

_TAG_KEY = 'created-by'
_TAG_VALUE = 'benchmark-toolkit'
_SG_NAME = 'benchmark-toolkit-sg'

# Confidential Computing support on AWS.
# Reference: https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/amd-sev-snp.html
#            https://docs.aws.amazon.com/enclaves/latest/user/nitro-enclave.html
_CC_TYPES = {
    'sevsnp':        'sev_snp',     # AMD SEV-SNP via --cpu-options AmdSevSnp=enabled
    'sev_snp':       'sev_snp',
    'amdsevsnp':     'sev_snp',
    'amd_sev_snp':   'sev_snp',
    'nitroenclave':  'nitro',       # AWS Nitro Enclaves
    'nitro_enclave': 'nitro',
    'nitro':         'nitro',
}
_CC_DEFAULT = 'sev_snp'
# Instance families that support AMD SEV-SNP on AWS.
_CC_SEV_SNP_FAMILIES = ('m6a', 'c6a', 'r6a', 'm7a', 'c7a', 'r7a', 'hpc7a')


class AWSProvider(CloudProvider):
    """Cloud provider backed by the aws CLI."""

    def __init__(self, profile: Optional[str] = None):
        self.profile = profile

    # ── Internal helpers ───────────────────────────────────────────────────────

    def _run(self, args: List[str], region: str = None,
             output: str = 'json', check: bool = True) -> object:
        cmd = ['aws'] + args + ['--output', output]
        if region:
            cmd += ['--region', region]
        if self.profile:
            cmd += ['--profile', self.profile]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if check and result.returncode != 0:
            raise RuntimeError(
                f"aws error (exit {result.returncode}):\n{result.stderr.strip()}"
            )
        if output == 'json':
            try:
                return json.loads(result.stdout) if result.stdout.strip() else {}
            except json.JSONDecodeError:
                return result.stdout
        return result.stdout.strip()

    def _get_ubuntu_ami(self, region: str) -> str:
        """Find the latest Ubuntu 22.04 LTS AMI ID via SSM parameter store."""
        ami_id = self._run(
            ['ssm', 'get-parameter',
             '--name', '/aws/service/canonical/ubuntu/server/22.04/stable/current/amd64/hvm/ebs-gp2/ami-id',
             '--query', 'Parameter.Value'],
            region=region, output='text', check=True,
        )
        if not ami_id or ami_id == 'None':
            raise RuntimeError(
                f"Could not find Ubuntu 22.04 AMI for region '{region}'. "
                "Specify --image <ami-id> explicitly."
            )
        return ami_id

    def _ensure_security_group(self, region: str) -> str:
        """Return (or create) a security group that allows inbound SSH."""
        result = self._run(
            ['ec2', 'describe-security-groups',
             '--filters', f'Name=group-name,Values={_SG_NAME}',
             '--query', 'SecurityGroups[0].GroupId'],
            region=region, output='text', check=False,
        )
        if result and result not in ('None', 'null', ''):
            return result

        print(f"[aws] Creating SSH security group '{_SG_NAME}' in {region}...")
        sg = self._run(
            ['ec2', 'create-security-group',
             '--group-name', _SG_NAME,
             '--description', 'Benchmark Toolkit – SSH access',
             '--tag-specifications',
             json.dumps([{
                 'ResourceType': 'security-group',
                 'Tags': [{'Key': _TAG_KEY, 'Value': _TAG_VALUE}],
             }])],
            region=region,
        )
        sg_id = sg['GroupId']
        self._run(
            ['ec2', 'authorize-security-group-ingress',
             '--group-id', sg_id,
             '--protocol', 'tcp', '--port', '22', '--cidr', '0.0.0.0/0'],
            region=region,
        )
        return sg_id

    @staticmethod
    def _instance_name(item: dict) -> str:
        return next(
            (t['Value'] for t in item.get('Tags', []) if t['Key'] == 'Name'),
            item['InstanceId'],
        )

    def _parse_instance(self, item: dict, region: str,
                        ssh_user: str, ssh_key_path: str) -> VMInstance:
        az = item.get('Placement', {}).get('AvailabilityZone', '')
        inst_region = az[:-1] if az else region
        return VMInstance(
            vm_id=item['InstanceId'],
            name=self._instance_name(item),
            public_ip=item.get('PublicIpAddress', ''),
            private_ip=item.get('PrivateIpAddress', ''),
            state=item.get('State', {}).get('Name', ''),
            provider='aws',
            region=inst_region,
            instance_type=item.get('InstanceType', ''),
            ssh_user=ssh_user,
            ssh_key_path=ssh_key_path,
            extra={'region': inst_region},
        )

    def _confidential_args(self, config: VMConfig) -> List[str]:
        """Return ec2 run-instances flags required for Confidential Computing."""
        if not config.confidential_compute:
            return []
        key = (config.confidential_type or _CC_DEFAULT).lower().replace('-', '').replace(' ', '')
        cc_kind = _CC_TYPES.get(key)
        if cc_kind is None:
            raise ValueError(
                f"Unknown AWS confidential type: {config.confidential_type!r}. "
                f"Choose from: SevSnp (AMD SEV-SNP) or NitroEnclave."
            )
        if cc_kind == 'sev_snp':
            family = config.instance_type.split('.')[0].lower()
            if family not in _CC_SEV_SNP_FAMILIES:
                print(
                    f"[aws] WARNING: AMD SEV-SNP is supported on "
                    f"{_CC_SEV_SNP_FAMILIES} instance families. "
                    f"Got '{config.instance_type}'. The API will reject this if incompatible."
                )
            print("[aws] Confidential Computing enabled: AMD SEV-SNP")
            return ['--cpu-options', 'AmdSevSnp=enabled']
        # Nitro Enclave
        print("[aws] Confidential Computing enabled: Nitro Enclave")
        return ['--enclave-options', json.dumps({'Enabled': True})]

    # ── CloudProvider interface ────────────────────────────────────────────────

    def create_vm(self, config: VMConfig) -> VMInstance:
        if not config.ssh_key_name:
            raise ValueError(
                "--aws-key-name is required for AWS.  "
                "Provide the name of an existing EC2 key pair "
                "(aws ec2 describe-key-pairs to list yours)."
            )

        name = config.vm_name or f'benchmark-{uuid.uuid4().hex[:8]}'
        key_path = os.path.expanduser(config.ssh_key_path or '~/.ssh/id_rsa')
        image_id = config.image or self._get_ubuntu_ami(config.region)
        sg_id = self._ensure_security_group(config.region)
        cc_args = self._confidential_args(config)

        print(f"[aws] Launching '{name}' ({config.instance_type}) in {config.region}...")
        data = self._run(
            ['ec2', 'run-instances',
             '--image-id', image_id,
             '--instance-type', config.instance_type,
             '--key-name', config.ssh_key_name,
             '--security-group-ids', sg_id,
             '--count', '1',
             '--block-device-mappings', json.dumps([{
                 'DeviceName': '/dev/sda1',
                 'Ebs': {
                     'VolumeSize': config.disk_size_gb,
                     'DeleteOnTermination': True,
                 },
             }]),
             '--tag-specifications', json.dumps([{
                 'ResourceType': 'instance',
                 'Tags': [
                     {'Key': 'Name', 'Value': name},
                     {'Key': _TAG_KEY, 'Value': _TAG_VALUE},
                 ],
             }])] + cc_args,
            region=config.region,
        )

        item = data['Instances'][0]
        return VMInstance(
            vm_id=item['InstanceId'],
            name=name,
            public_ip='',           # not yet assigned
            private_ip=item.get('PrivateIpAddress', ''),
            state='pending',
            provider='aws',
            region=config.region,
            instance_type=config.instance_type,
            ssh_user=config.ssh_user,
            ssh_key_path=key_path,
            extra={'region': config.region},
        )

    def get_vm(self, vm_id: str, extra: Dict = None) -> VMInstance:
        extra = extra or {}
        region = extra.get('region', 'us-east-1')
        data = self._run(
            ['ec2', 'describe-instances', '--instance-ids', vm_id],
            region=region,
        )
        reservations = data.get('Reservations', [])
        if not reservations:
            raise RuntimeError(f"Instance '{vm_id}' not found in {region}")
        item = reservations[0]['Instances'][0]
        return self._parse_instance(
            item, region,
            ssh_user=extra.get('ssh_user', 'ubuntu'),
            ssh_key_path=extra.get('ssh_key_path', '~/.ssh/id_rsa'),
        )

    def delete_vm(self, instance: VMInstance) -> None:
        region = instance.extra.get('region', 'us-east-1')
        print(f"[aws] Terminating instance '{instance.vm_id}' in {region}...")
        self._run(
            ['ec2', 'terminate-instances', '--instance-ids', instance.vm_id],
            region=region,
        )

    def list_vms(self) -> List[VMInstance]:
        """List benchmark VMs in the default (or profile-configured) region."""
        data = self._run(
            ['ec2', 'describe-instances',
             '--filters',
             f'Name=tag:{_TAG_KEY},Values={_TAG_VALUE}',
             'Name=instance-state-name,Values=pending,running,stopping,stopped'],
            check=False,
        )
        result = []
        for reservation in data.get('Reservations', []):
            for item in reservation.get('Instances', []):
                result.append(
                    self._parse_instance(item, '', 'ubuntu', '~/.ssh/id_rsa')
                )
        return result
