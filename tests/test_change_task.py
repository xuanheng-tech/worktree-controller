"""Preserve first-use workspaces and exercise real delivery with explicit companion tools."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "examples/change_task.py"


def environment(home, bin_dir):
    return {"PATH": str(bin_dir) + os.pathsep + os.defpath, "HOME": str(home), "LANG": "C.UTF-8",
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0", "PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1"}


@pytest.fixture
def tools(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in ("worktree-controller", "worktree-session-end", "git-finalize", "snapshot-runner"):
        path = bin_dir / name
        path.write_text("#!/bin/sh\nexit 97\n")
        path.chmod(0o755)
    return bin_dir


def invoke(tmp_path, tools, *arguments, optimize=False):
    return subprocess.run([sys.executable, *(["-O"] if optimize else []), str(SCRIPT),
                           "--finalizer", str(tools / "git-finalize"),
                           "--snapshot-runner", str(tools / "snapshot-runner"), *map(str, arguments)],
                          env=environment(tmp_path, tools), capture_output=True, text=True,
                          timeout=20, check=False)


@pytest.mark.parametrize("optimize", [False, True])
def test_existing_workspace_is_untouched(tmp_path, tools, optimize):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    note = workspace / "owner-note.txt"
    note.write_bytes(b"keep owner data\x00")
    result = invoke(tmp_path, tools, "--workspace", workspace, optimize=optimize)
    assert result.returncode == 2 and "must be a new path" in result.stderr
    assert list(workspace.iterdir()) == [note] and note.read_bytes() == b"keep owner data\x00"


@pytest.mark.parametrize("name", ["git-finalize", "snapshot-runner"])
def test_missing_companion_stops_before_workspace_creation(tmp_path, tools, name):
    (tools / name).unlink()
    workspace = tmp_path / "workspace"
    result = invoke(tmp_path, tools, "--workspace", workspace)
    assert result.returncode == 2 and "install" in result.stderr
    assert not workspace.exists()


def test_resume_without_checkpoint_keeps_owner_files(tmp_path, tools):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    note = workspace / "keep.txt"
    note.write_text("unfinished work")
    result = invoke(tmp_path, tools, "--workspace", workspace, "--resume")
    assert result.returncode == 2 and "handoff.json" in result.stderr
    assert list(workspace.iterdir()) == [note] and note.read_text() == "unfinished work"


def test_resume_rejects_symlink_checkpoint_without_reading_its_target(tmp_path, tools):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    foreign = tmp_path / "foreign.json"
    foreign.write_text("unrelated owner data")
    (workspace / "handoff.json").symlink_to(foreign)
    result = invoke(tmp_path, tools, "--workspace", workspace, "--resume")
    assert result.returncode == 1 and "must not be symlinks" in result.stderr
    assert foreign.read_text() == "unrelated owner data" and not (workspace / "commands.jsonl").exists()


@pytest.mark.skipif(not (os.environ.get("WC_DEMO_FINALIZER") and os.environ.get("WC_DEMO_SNAPSHOT_RUNNER")),
                    reason="opt in with installed public Finalizer and Snapshot Runner entrypoints")
def test_real_pause_refusals_resume_and_completed_replay(tmp_path):
    workspace = tmp_path / "demo"
    command = [sys.executable, str(SCRIPT), "--workspace", str(workspace),
               "--finalizer", os.environ["WC_DEMO_FINALIZER"],
               "--snapshot-runner", os.environ["WC_DEMO_SNAPSHOT_RUNNER"]]
    env = environment(tmp_path, Path(sys.executable).parent)

    def demo(*extra):
        result = subprocess.run([*command, *extra], env=env, capture_output=True, text=True,
                                timeout=180, check=False)
        return result

    def head():
        return subprocess.check_output(["git", "--git-dir", str(workspace / "origin.git"),
                                        "rev-parse", "refs/heads/main"], env=env, text=True).strip()

    paused = demo("--pause-after-handoff")
    assert paused.returncode == 0, paused.stdout + paused.stderr
    assert "PAUSED:" in paused.stdout and "PASS:" not in paused.stdout
    initial = head()
    checkpoint = workspace / "handoff.json"
    original = checkpoint.read_bytes()
    saved = json.loads(original)
    note = workspace / "change/owner-note.txt"
    note.write_bytes(b"unknown to the example\x00")
    refused = demo("--resume")
    assert refused.returncode == 1 and "uncommitted files remain" in refused.stderr
    assert head() == initial and note.read_bytes() == b"unknown to the example\x00"
    # The test owns this fixture file and keeps its bytes outside the checkout.
    preserved = tmp_path / "preserved-owner-note.txt"
    note.rename(preserved)
    checkpoint.write_text(json.dumps(dict(saved, source_commit="0" * 40)))
    drifted = demo("--resume")
    assert drifted.returncode == 1 and "source identity changed" in drifted.stderr
    assert head() == initial
    checkpoint.write_bytes(original)
    resumed = demo("--resume")
    assert resumed.returncode == 0, resumed.stdout + resumed.stderr
    assert "PASS:" in resumed.stdout and head() == saved["source_commit"]
    assert not (workspace / "change").exists() and not (workspace / "integration").exists()
    assert json.loads((workspace / "closeout.json").read_text())["summary"]["terminal_complete"] is True
    commands = [json.loads(line) for line in (workspace / "commands.jsonl").read_text().splitlines()]
    publications = sum("--publish-integration-candidate" in row["argv"] for row in commands)
    replayed = demo("--resume")
    assert replayed.returncode == 0, replayed.stdout + replayed.stderr
    after = [json.loads(line) for line in (workspace / "commands.jsonl").read_text().splitlines()]
    assert sum("--publish-integration-candidate" in row["argv"] for row in after) == publications == 1
    assert head() == saved["source_commit"] and checkpoint.read_bytes() == original
    assert preserved.read_bytes() == b"unknown to the example\x00"
