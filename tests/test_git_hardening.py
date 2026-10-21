"""M3: Git subprocess hardening at the single _git choke point."""

import subprocess
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Event, Thread

import pytest

from tests.test_shadow import GitRepository, git
from worktree_controller import git_backend
from worktree_controller.git_backend import _git
from worktree_controller.models import ControllerError


def test_git_invokes_with_timeout_stdin_isolation_and_no_prompts(monkeypatch):
    captured = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured.update(kwargs)
        return subprocess.CompletedProcess(command, 0, stdout=b"ok\n", stderr=b"")

    monkeypatch.setattr(git_backend.subprocess, "run", fake_run)
    result = _git(Path("/repo"), "status", "--porcelain")

    assert result.stdout == b"ok\n"
    assert captured["timeout"] == git_backend.GIT_COMMAND_TIMEOUT_SECONDS
    assert captured["stdin"] is subprocess.DEVNULL
    environment = captured["env"]
    assert environment["GIT_TERMINAL_PROMPT"] == "0"
    assert environment["GIT_ASKPASS"] == "/bin/false"
    assert environment["SSH_ASKPASS"] == "/bin/false"
    assert environment["GIT_OPTIONAL_LOCKS"] == "0"
    assert environment["GIT_NO_REPLACE_OBJECTS"] == "1"


def test_git_timeout_maps_to_a_stable_error(monkeypatch):
    def hang(command, **kwargs):
        raise subprocess.TimeoutExpired(command, kwargs.get("timeout", 0))

    monkeypatch.setattr(git_backend.subprocess, "run", hang)
    with pytest.raises(ControllerError) as blocked:
        _git(Path("/repo"), "fetch", "origin")
    assert blocked.value.code == "GIT_TIMEOUT"


def test_timeout_treats_check_false_the_same_way(monkeypatch):
    def hang(command, **kwargs):
        raise subprocess.TimeoutExpired(command, 1)

    monkeypatch.setattr(git_backend.subprocess, "run", hang)
    with pytest.raises(ControllerError) as blocked:
        _git(Path("/repo"), "status", check=False)
    assert blocked.value.code == "GIT_TIMEOUT"


def test_a_wedged_remote_fails_closed_within_the_bound(monkeypatch):
    """A local bounded fixture stalls HTTP independently of external networking."""
    finished = Event()
    requested = Event()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requested.set()
            finished.wait(5)

        def log_message(self, *args):
            pass

    for name in (
        "HTTP_PROXY",
        "http_proxy",
        "HTTPS_PROXY",
        "https_proxy",
        "ALL_PROXY",
        "all_proxy",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    monkeypatch.setenv("no_proxy", "127.0.0.1")
    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with GitRepository(linked=0) as repository:
                git(repository.repo, "config", "http.proxy", "")
                git(
                    repository.repo,
                    "remote",
                    "add",
                    "slow",
                    f"http://127.0.0.1:{server.server_port}/x.git",
                )
                started = time.monotonic()
                with pytest.raises(ControllerError) as blocked:
                    _git(repository.repo, "fetch", "slow", timeout=1)
                assert requested.is_set()
                assert blocked.value.code == "GIT_TIMEOUT"
                assert time.monotonic() - started < 10
                assert _git(repository.repo, "rev-parse", "HEAD").returncode == 0
        finally:
            finished.set()
            server.shutdown()
            thread.join(timeout=5)
