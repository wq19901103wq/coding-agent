import pytest
from pathlib import Path

from agent.safety import (
    CommandClass,
    PathOutsideWorkspaceError,
    classify_shell_command,
    validate_path,
)


class TestValidatePath:
    def test_inside_workspace(self, tmp_path):
        target = validate_path("src/main.py", tmp_path)
        assert target == tmp_path.resolve() / "src" / "main.py"

    def test_current_directory(self, tmp_path):
        target = validate_path("./main.py", tmp_path)
        assert target == tmp_path.resolve() / "main.py"

    def test_workspace_root(self, tmp_path):
        target = validate_path(".", tmp_path)
        assert target == tmp_path.resolve()

    def test_outside_via_parent(self, tmp_path):
        with pytest.raises(PathOutsideWorkspaceError):
            validate_path("../secret.txt", tmp_path)

    def test_absolute_outside(self, tmp_path):
        with pytest.raises(PathOutsideWorkspaceError):
            validate_path("/etc/passwd", tmp_path)

    def test_symlink_outside_workspace(self, tmp_path):
        outside = tmp_path.parent / "outside_secret.txt"
        outside.write_text("secret")
        link = tmp_path / "link.txt"
        link.symlink_to(outside)
        with pytest.raises(PathOutsideWorkspaceError):
            validate_path("link.txt", tmp_path)


class TestClassifyShellCommand:
    def test_ls_is_harmless(self):
        assert classify_shell_command("ls -la") == CommandClass.HARMLESS

    def test_pipe_between_readonly_commands_is_harmless(self):
        assert classify_shell_command("cat a.py | grep def") == CommandClass.HARMLESS

    def test_redirect_is_dangerous(self):
        assert classify_shell_command("echo x > a.py") == CommandClass.DANGEROUS

    def test_command_combination_is_dangerous(self):
        assert classify_shell_command("ls && rm a.py") == CommandClass.DANGEROUS

    def test_install_is_dangerous(self):
        assert classify_shell_command("pip install requests") == CommandClass.DANGEROUS

    def test_network_is_dangerous(self):
        assert classify_shell_command("curl https://example.com") == CommandClass.DANGEROUS

    def test_write_operation_is_dangerous(self):
        assert classify_shell_command("rm a.py") == CommandClass.DANGEROUS

    def test_sudo_is_forbidden(self):
        assert classify_shell_command("sudo ls") == CommandClass.FORBIDDEN

    def test_rm_rf_root_is_forbidden(self):
        assert classify_shell_command("rm -rf /") == CommandClass.FORBIDDEN

    def test_outside_read_is_forbidden(self):
        assert classify_shell_command("cat ../x.txt") == CommandClass.FORBIDDEN

    def test_unknown_command_is_dangerous(self):
        assert classify_shell_command("unknown") == CommandClass.DANGEROUS

    def test_empty_command_is_dangerous(self):
        assert classify_shell_command("") == CommandClass.DANGEROUS
