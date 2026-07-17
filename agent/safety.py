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
    "wc",
    "stat",
}

GIT_HARMLESS_SUBCOMMANDS = {"status", "log", "diff", "show"}

PYTHON_DANGEROUS_PATTERNS = [
    r"\.\s*write\s*\(",
    r"open\s*\([^)]*['\"][wa]",
    r"\bos\.system\s*\(",
    r"\bsubprocess\.(call|run|popen)\s*\(",
    r"\beval\s*\(",
    r"\bexec\s*\(",
    r"\b__import__\s*\(",
    r"\bimport\s+os\b",
    r"\bimport\s+subprocess\b",
    r"\bimport\s+shutil\b",
    r"\bimport\s+socket\b",
    r"\bimport\s+sys\b",
    r"\bimport\s+pathlib\b",
    r"\bfrom\s+os\b",
    r"\bfrom\s+subprocess\b",
    r"\bfrom\s+shutil\b",
    r"\bfrom\s+socket\b",
    r"\bfrom\s+sys\b",
    r"\bfrom\s+pathlib\b",
]

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
        raise PathOutsideWorkspaceError(f"Path outside workspace: '{path}'")
    return target


def _segment_is_harmless(segment: str) -> bool:
    try:
        parts = shlex.split(segment.strip())
    except ValueError:
        return False
    if not parts:
        return False
    return parts[0] in HARMLESS_COMMANDS


def _git_command_is_harmless(command: str) -> bool:
    try:
        parts = shlex.split(command.strip())
    except ValueError:
        return False
    if len(parts) < 2:
        return False
    if parts[0] != "git":
        return False
    return parts[1] in GIT_HARMLESS_SUBCOMMANDS


def _python_c_is_harmless(command: str) -> bool:
    """Return True for python -c that does not use dangerous I/O or execution patterns."""
    try:
        parts = shlex.split(command.strip())
    except ValueError:
        return False
    if len(parts) < 3:
        return False
    if parts[0] not in {"python", "python3"}:
        return False
    if "-c" not in parts:
        return False
    idx = parts.index("-c")
    code = " ".join(parts[idx + 1 :])
    for pat in PYTHON_DANGEROUS_PATTERNS:
        if re.search(pat, code):
            return False
    return True


_HARMLESS_PYTHON_MODULES = {"pytest", "py_compile", "compileall", "json", "sys", "ast"}


def _python_module_command_is_harmless(command: str) -> bool:
    """Return True for python -m pytest / py_compile / compileall invocations."""
    try:
        parts = shlex.split(command.strip())
    except ValueError:
        return False
    if len(parts) < 3:
        return False
    if parts[0] not in {"python", "python3"}:
        return False
    if parts[1] != "-m":
        return False
    module = parts[2]
    return module in _HARMLESS_PYTHON_MODULES or module.startswith("pytest.")


def classify_shell_command(command: str) -> CommandClass:
    cmd = command.strip().lower()
    if not cmd:
        return CommandClass.DANGEROUS

    for pat in FORBIDDEN_PATTERNS:
        if re.search(pat, cmd):
            return CommandClass.FORBIDDEN

    if _git_command_is_harmless(cmd):
        return CommandClass.HARMLESS

    if _python_c_is_harmless(cmd):
        return CommandClass.HARMLESS

    if _python_module_command_is_harmless(cmd):
        return CommandClass.HARMLESS

    for pat in DANGEROUS_PATTERNS:
        if re.search(pat, cmd):
            return CommandClass.DANGEROUS

    segments = cmd.split("|")
    if all(_segment_is_harmless(seg) for seg in segments):
        return CommandClass.HARMLESS

    return CommandClass.DANGEROUS
