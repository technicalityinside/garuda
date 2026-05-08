from .base import VMConfig, VMInstance, CloudProvider
from .runner import CloudBenchmarkRunner
from .remote import RemoteExecutor
from .state import VMStateStore


def get_provider(name: str, **kwargs) -> CloudProvider:
    """Factory that returns the correct CloudProvider by name."""
    key = name.lower().strip()
    if key == 'gcp':
        from .gcp import GCPProvider
        return GCPProvider(project=kwargs.get('project'))
    if key == 'azure':
        from .azure import AzureProvider
        return AzureProvider(subscription=kwargs.get('subscription'))
    if key in ('aws', 'amazon'):
        from .aws import AWSProvider
        return AWSProvider(profile=kwargs.get('profile'))
    raise ValueError(
        f"Unknown provider: {name!r}.  Choose from: gcp, azure, aws"
    )


__all__ = [
    'VMConfig', 'VMInstance', 'CloudProvider',
    'CloudBenchmarkRunner', 'RemoteExecutor', 'VMStateStore',
    'get_provider',
]
