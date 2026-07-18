"""Small, explicit project-memory layer for interactive agents.

Shared repository instructions stay in familiar files such as ``AGENTS.md``.
Private facts are stored outside the repository so they cannot be committed by
accident.  Memory writes are deliberately explicit; this module never asks an
LLM to extract or save facts automatically.
"""

from __future__ import annotations

import hashlib
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

_SECRET_PATTERNS = (
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----", re.IGNORECASE),
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),
    re.compile(
        r"\b(?:api[_-]?key|access[_-]?token|password|secret)\s*[:=]\s*\S{8,}",
        re.IGNORECASE,
    ),
    re.compile(r"\bauthorization\s*:\s*bearer\s+\S+", re.IGNORECASE),
)


@dataclass(frozen=True)
class MemorySource:
    """A rendered memory source and its user-facing label."""

    label: str
    path: Path
    content: str


class MemoryManager:
    """Load shared instructions and manage private per-project memory."""

    SHARED_FILENAMES = ("AGENTS.md", "CLAUDE.md")

    def __init__(
        self,
        workspace: str | Path,
        *,
        enabled: bool = True,
        max_chars: int = 12_000,
        storage_root: str | Path = "~/.coding-agent/projects",
    ) -> None:
        self.workspace = Path(workspace).resolve()
        self.enabled = enabled
        self.max_chars = max_chars
        self.storage_root = Path(storage_root).expanduser()

    @property
    def private_path(self) -> Path:
        digest = hashlib.sha256(self._project_identity().encode("utf-8")).hexdigest()[:16]
        return self.storage_root / digest / "memory" / "MEMORY.md"

    @property
    def _legacy_private_path(self) -> Path:
        digest = hashlib.sha256(str(self.workspace).encode("utf-8")).hexdigest()[:16]
        return self.storage_root / digest / "memory.md"

    def _read_private_path(self) -> Path:
        if self.private_path.is_file():
            return self.private_path
        return self._legacy_private_path

    def _project_identity(self) -> str:
        """Return a stable identity shared by worktrees of the same repository."""
        try:
            result = subprocess.run(
                ["git", "-C", str(self.workspace), "rev-parse", "--git-common-dir"],
                capture_output=True,
                text=True,
                timeout=2,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return str(self.workspace)
        if result.returncode != 0 or not result.stdout.strip():
            return str(self.workspace)
        common_dir = Path(result.stdout.strip())
        if not common_dir.is_absolute():
            common_dir = self.workspace / common_dir
        return str(common_dir.resolve())

    def load_sources(self) -> list[MemorySource]:
        if not self.enabled:
            return []

        candidates: list[tuple[str, Path]] = [
            ("全局 AGENTS.md", Path.home() / ".coding-agent" / "AGENTS.md"),
        ]
        candidates.extend((name, self.workspace / name) for name in self.SHARED_FILENAMES)
        candidates.append(("项目私有记忆", self._read_private_path()))

        sources: list[MemorySource] = []
        seen: set[Path] = set()
        for label, path in candidates:
            resolved = path.resolve()
            if resolved in seen or not path.is_file():
                continue
            seen.add(resolved)
            try:
                content = path.read_text(encoding="utf-8").strip()
            except (OSError, UnicodeError):
                continue
            if content:
                sources.append(MemorySource(label=label, path=path, content=content))
        return sources

    def render_context(self) -> str:
        """Render bounded memory context suitable for a system prompt."""
        if not self.enabled:
            return ""
        chunks: list[str] = []
        used = 0
        for source in self.load_sources():
            header = f"### {source.label}\n"
            separator_length = 2 if chunks else 0
            remaining = self.max_chars - used - separator_length - len(header)
            if remaining <= 0:
                break
            content = source.content
            if source.label == "项目私有记忆":
                content = "\n".join(content.splitlines()[:200])
            if len(content) > remaining:
                marker = "\n[内容已按记忆上限截断]"
                if remaining <= len(marker):
                    content = marker[:remaining]
                else:
                    content = content[: remaining - len(marker)].rstrip() + marker
            chunk = header + content
            chunks.append(chunk)
            used += separator_length + len(chunk)
            if used >= self.max_chars:
                break
        return "\n\n".join(chunks)

    def list_entries(self) -> list[str]:
        """Return explicit private-memory entries in storage order."""
        path = self._read_private_path()
        if not path.is_file():
            return []
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError):
            return []
        return [line[2:].strip() for line in lines if line.startswith("- ") and line[2:].strip()]

    def add(self, text: str) -> bool:
        """Add a normalized entry. Return False when it already exists."""
        entry = " ".join(text.split())
        if not entry:
            raise ValueError("记忆内容不能为空")
        if any(pattern.search(entry) for pattern in _SECRET_PATTERNS):
            raise ValueError("检测到疑似密钥或密码，拒绝写入项目记忆")

        entries = self.list_entries()
        if entry in entries:
            return False
        entries.append(entry)
        self._write_entries(entries)
        return True

    def remove(self, index: int) -> str:
        entries = self.list_entries()
        if index < 1 or index > len(entries):
            raise IndexError("记忆序号不存在")
        removed = entries.pop(index - 1)
        self._write_entries(entries)
        return removed

    def clear(self) -> int:
        entries = self.list_entries()
        for path in (self.private_path, self._legacy_private_path):
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        return len(entries)

    def _write_entries(self, entries: list[str]) -> None:
        path = self.private_path
        if not entries:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            return

        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.parent.chmod(0o700)
        content = (
            "# coding-agent 项目记忆\n\n" + "\n".join(f"- {entry}" for entry in entries) + "\n"
        )
        if len(content) > self.max_chars:
            raise ValueError(f"项目记忆超过 {self.max_chars} 字符上限")
        temporary = path.with_suffix(".tmp")
        temporary.write_text(content, encoding="utf-8")
        temporary.chmod(0o600)
        temporary.replace(path)
        path.chmod(0o600)
