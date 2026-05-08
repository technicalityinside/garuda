import json
import os
from datetime import datetime
from typing import List, Optional

from .base import VMInstance

_DEFAULT_STATE_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'results', 'cloud_vms.json',
)


class VMStateStore:
    """Persists VM metadata to a local JSON file so VMs can be referenced by name."""

    def __init__(self, path: str = None):
        self.path = path or _DEFAULT_STATE_FILE

    # ── Persistence ────────────────────────────────────────────────────────────

    def _load(self) -> dict:
        if not os.path.exists(self.path):
            return {'vms': {}}
        with open(self.path) as f:
            return json.load(f)

    def _save(self, data: dict) -> None:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, 'w') as f:
            json.dump(data, f, indent=2)

    # ── Public API ─────────────────────────────────────────────────────────────

    def save(self, instance: VMInstance) -> None:
        data = self._load()
        entry = instance.to_dict()
        entry['created_at'] = datetime.now().isoformat()
        data['vms'][instance.name] = entry
        self._save(data)

    def get(self, name: str) -> Optional[VMInstance]:
        data = self._load()
        entry = data['vms'].get(name)
        return VMInstance.from_dict(entry) if entry else None

    def remove(self, name: str) -> None:
        data = self._load()
        data['vms'].pop(name, None)
        self._save(data)

    def list_all(self) -> List[VMInstance]:
        data = self._load()
        return [VMInstance.from_dict(v) for v in data['vms'].values()]
