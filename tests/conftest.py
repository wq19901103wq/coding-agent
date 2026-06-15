import pytest


@pytest.fixture
def isolated_home(monkeypatch, tmp_path):
    """提供一个隔离的 HOME 目录，并将当前工作目录切换到该目录。"""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    return tmp_path
