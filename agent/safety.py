import re
import shlex
from enum import Enum
from pathlib import Path


class CommandClass(Enum):
    HARMLESS = "harmless"
    DANGEROUS = "dangerous"
    FORBIDDEN = "forbidden"


class PathOutsideWorkspaceError(Exception):
    pass


HARMLESS_COMMANDS = {
    "ls",
    "cat",
    "head",
    "tail",
    "less",
    "grep",
    "find",
    "rg",
    "awk",
    "pwd",
    "echo",
    "which",
}

DANGEROUS_PATTERNS = [
    r"\brm\b",
    r"\bcp\b",
    r"\bmv\b",
    r"\bmkdir\b",
    r"\btouch\b",
    r"\btee\b",
    r"pip\s+install",
    r"brew\s+install",
    r"npm\s+install",
    r"\bapt-get\b",
    r"\bcurl\b",
    r"\bwget\b",
    r"\bssh\b",
    r"\bscp\b",
    r"\bkill\b",
    r"\bchmod\b",
    r"\bchown\b",
    r"\bsystemctl\b",
    r"\bbash\b",
    r"\bsh\b",
    r"[>]",
    r"\$\(",
    r"&&",
    r"\|\|",
    r";",
]

FORBIDDEN_PATTERNS = [
    r"\bsudo\b",
    r"\bsu\b",
    r"\bdoas\b",
    r"\brm\s+-rf\s+/",
    r"\bdd\b",
    r"\bmkfs\b",
    r"\.\./",
    r"/etc/passwd",
    r"~/.ssh",
    r"~/.bashrc",
    r"/etc\b",
    r"/usr/bin\b",
    r"/bin\b",
]


def validate_path(path: str, workspace: Path) -> Path:
    target = (workspace / path).resolve()
    resolved_ws = workspace.resolve()
    try:
        target.relative_to(resolved_ws)
    except ValueError:
        raise PathOutsideWorkspaceError(f"Path '{path}' is outside workspace")
    return target


def _segment_is_harmless(segment: str) -> bool:
    try:
        parts = shlex.split(segment.strip())
    except ValueError:
        return False
    if not parts:
        return False
    return parts[0] in HARMLESS_COMMANDS


def classify_shell_command(command: str) -> CommandClass:
    cmd = command.strip().lower()
    if not cmd:
        return CommandClass.DANGEROUS

    for pat in FORBIDDEN_PATTERNS:
        if re.search(pat, cmd):
            return CommandClass.FORBIDDEN

    for pat in DANGEROUS_PATTERNS:
        if re.search(pat, cmd):
            return CommandClass.DANGEROUS

    segments = cmd.split("|")
    if all(_segment_is_harmless(seg) for seg in segments):
        return CommandClass.HARMLESS

    return CommandClass.DANGEROUS
