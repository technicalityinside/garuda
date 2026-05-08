from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
import socket
import time


@dataclass
class VMConfig:
    """Desired configuration for a new cloud VM."""
    provider: str
    region: str
    instance_type: str
    image: Optional[str] = None          # defaults to Ubuntu 22.04 LTS per provider
    disk_size_gb: int = 50
    ssh_user: str = 'ubuntu'
    ssh_key_path: Optional[str] = None   # local private key path; ~/.ssh/id_rsa default
    ssh_key_name: Optional[str] = None   # AWS EC2 key-pair name
    vm_name: Optional[str] = None        # auto-generated if None
    zone: Optional[str] = None           # GCP only; defaults to {region}-a
    resource_group: Optional[str] = None # Azure only
    tags: Dict[str, str] = field(default_factory=dict)


@dataclass
class VMInstance:
    """Live state of a cloud VM."""
    vm_id: str
    name: str
    public_ip: str
    private_ip: str
    state: str
    provider: str
    region: str
    instance_type: str
    ssh_user: str
    ssh_key_path: str
    extra: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict:
        return {
            'vm_id': self.vm_id,
            'name': self.name,
            'public_ip': self.public_ip,
            'private_ip': self.private_ip,
            'state': self.state,
            'provider': self.provider,
            'region': self.region,
            'instance_type': self.instance_type,
            'ssh_user': self.ssh_user,
            'ssh_key_path': self.ssh_key_path,
            'extra': self.extra,
        }

    @classmethod
    def from_dict(cls, d: Dict) -> 'VMInstance':
        return cls(
            vm_id=d['vm_id'],
            name=d['name'],
            public_ip=d['public_ip'],
            private_ip=d['private_ip'],
            state=d['state'],
            provider=d['provider'],
            region=d['region'],
            instance_type=d['instance_type'],
            ssh_user=d['ssh_user'],
            ssh_key_path=d['ssh_key_path'],
            extra=d.get('extra', {}),
        )


_RUNNING_STATES = frozenset({'RUNNING', 'running', 'VM running'})


class CloudProvider(ABC):
    """Abstract interface for cloud VM lifecycle management."""

    @abstractmethod
    def create_vm(self, config: VMConfig) -> VMInstance:
        """Submit VM creation request. The instance may still be starting."""
        ...

    @abstractmethod
    def get_vm(self, vm_id: str, extra: Dict = None) -> VMInstance:
        """Fetch current state of a VM."""
        ...

    @abstractmethod
    def delete_vm(self, instance: VMInstance) -> None:
        """Terminate and delete a VM (and its associated resources)."""
        ...

    @abstractmethod
    def list_vms(self) -> List[VMInstance]:
        """Return all VMs created by this toolkit (filtered by tag)."""
        ...

    def wait_for_ready(self, instance: VMInstance, timeout: int = 300) -> VMInstance:
        """Poll until the VM is running and has a public IP."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                updated = self.get_vm(instance.vm_id, instance.extra)
                if updated.public_ip and updated.state in _RUNNING_STATES:
                    return updated
            except Exception:
                pass
            time.sleep(10)
        raise TimeoutError(
            f"VM '{instance.name}' did not reach running state within {timeout}s"
        )

    def wait_for_ssh(self, instance: VMInstance, timeout: int = 300) -> bool:
        """Poll TCP port 22 until SSH is accepting connections."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                with socket.create_connection((instance.public_ip, 22), timeout=5):
                    time.sleep(5)   # let sshd finish initializing
                    return True
            except OSError:
                time.sleep(10)
        return False
