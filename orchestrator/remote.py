import os
import subprocess
from typing import Tuple


class RemoteExecutor:
    """Runs commands and transfers files on a remote host over SSH."""

    def __init__(self, host: str, user: str, key_path: str, port: int = 22):
        self.host = host
        self.user = user
        self.key_path = os.path.expanduser(key_path)
        self.port = port

    # ── Internal helpers ───────────────────────────────────────────────────────

    def _ssh_opts(self) -> list:
        return [
            '-o', 'StrictHostKeyChecking=no',
            '-o', 'UserKnownHostsFile=/dev/null',
            '-o', 'ConnectTimeout=15',
            '-o', 'ServerAliveInterval=30',
            '-o', 'BatchMode=yes',
            '-i', self.key_path,
            '-p', str(self.port),
        ]

    def _ssh_opts_str(self) -> str:
        """Single string form of SSH options, for use inside rsync -e."""
        tokens = []
        opts = self._ssh_opts()
        i = 0
        while i < len(opts):
            flag = opts[i]
            if flag in ('-o', '-i', '-p'):
                tokens.append(f'{flag} {opts[i + 1]}')
                i += 2
            else:
                tokens.append(flag)
                i += 1
        return 'ssh ' + ' '.join(tokens)

    # ── Public API ─────────────────────────────────────────────────────────────

    def run(self, command: str, timeout: int = 300) -> Tuple[str, str, int]:
        """Run a shell command on the remote host. Returns (stdout, stderr, returncode)."""
        cmd = ['ssh'] + self._ssh_opts() + [f'{self.user}@{self.host}', command]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
            return result.stdout, result.stderr, result.returncode
        except subprocess.TimeoutExpired:
            raise TimeoutError(f"SSH command timed out after {timeout}s: {command[:100]}")

    def copy_to(self, local_path: str, remote_path: str) -> None:
        """Rsync a local directory to a remote path, skipping build artefacts."""
        cmd = [
            'rsync', '-avz',
            '-e', self._ssh_opts_str(),
            '--exclude=__pycache__',
            '--exclude=*.pyc',
            '--exclude=.git',
            '--exclude=results',
            '--exclude=.claude',
            '--exclude=output.csv',
            '--exclude=bin',
            local_path.rstrip('/') + '/',
            f'{self.user}@{self.host}:{remote_path}',
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(f"rsync to remote failed:\n{result.stderr}")

    def copy_from(self, remote_path: str, local_path: str) -> None:
        """Rsync a remote directory to a local path."""
        os.makedirs(local_path, exist_ok=True)
        cmd = [
            'rsync', '-avz',
            '-e', self._ssh_opts_str(),
            f'{self.user}@{self.host}:{remote_path.rstrip("/")}/',
            local_path.rstrip('/') + '/',
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(f"rsync from remote failed:\n{result.stderr}")

    def test_connection(self, timeout: int = 15) -> bool:
        try:
            _, _, rc = self.run('echo ok', timeout=timeout)
            return rc == 0
        except Exception:
            return False
