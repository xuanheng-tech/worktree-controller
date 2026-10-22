"""Real independent CLI processes preserve a task and reject incomplete settlement."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "examples/client_continuation.py"


def invoke(workspace, *arguments):
    environment = {"PATH": str(Path(sys.executable).parent) + os.pathsep + os.defpath,
                   "HOME": str(workspace.parent), "LANG": "C.UTF-8", "PYTHONNOUSERSITE": "1",
                   "PYTHONDONTWRITEBYTECODE": "1", "GIT_CONFIG_NOSYSTEM": "1",
                   "GIT_CONFIG_GLOBAL": os.devnull, "GIT_TERMINAL_PROMPT": "0"}
    return subprocess.run([sys.executable, "-O", str(SCRIPT), "--workspace", str(workspace), *arguments],
                          env=environment, capture_output=True, text=True, timeout=180, check=False)


def test_two_real_client_processes_and_refused_drift_preserve_original_task(tmp_path):
    workspace = tmp_path / "example"
    paused = invoke(workspace)
    assert paused.returncode == 0, paused.stdout + paused.stderr
    assert "PAUSED:" in paused.stdout and "PASS:" not in paused.stdout
    original_checkpoint = (workspace / "task-checkpoint.json").read_bytes()
    saved = json.loads(original_checkpoint)
    checkout = workspace / "continued-work"
    unknown = checkout / "owner-note.bin"
    original_note = unknown.read_bytes()
    incomplete = json.loads((workspace / "a-incomplete.json").read_text())
    assert incomplete["summary"]["terminal_complete"] is False
    summary = json.loads((workspace / "a-closeout.json").read_text())["summary"]
    assert summary["complete"] == summary["pending"] == 1 and summary["terminal_complete"] is False
    unknown.write_bytes(original_note + b"owner changed this")
    refused = invoke(workspace, "--resume")
    assert refused.returncode == 1 and "unfinished file changed" in refused.stderr
    assert unknown.read_bytes() == original_note + b"owner changed this"
    unknown.write_bytes(original_note)  # Only this test's exact fixture is restored.
    preserved = tmp_path / "fixture-note"
    unknown.rename(preserved)
    unknown.symlink_to(preserved)
    refused_link = invoke(workspace, "--resume")
    assert refused_link.returncode == 1 and "regular non-symlink file" in refused_link.stderr
    assert preserved.read_bytes() == original_note and unknown.is_symlink()
    unknown.unlink()  # Only the symlink created by this test is removed.
    preserved.rename(unknown)
    resumed = invoke(workspace, "--resume")
    assert resumed.returncode == 0, resumed.stdout + resumed.stderr
    result = json.loads((workspace / "result.json").read_text())
    assert result["protocol_acceptance_complete"] is True
    assert result["business_task_completed"] is result["published"] is False
    assert result["checkout_retained"] is True
    assert result["session_closeout_complete"] is False
    assert result["allocation_id"] == saved["task"]["allocation_id"]
    assert result["first_holder"]["pid"] != result["second_holder"]["pid"]
    assert result["first_holder"]["client_id"] != result["second_holder"]["client_id"]
    assert unknown.read_bytes() == original_note
    assert (workspace / "preserved-generated.bin").read_bytes() == b"example-owned generated fixture\x00"
    assert "Client B continued" in (checkout / "README.md").read_text()
    assert (workspace / "task-checkpoint.json").read_bytes() == original_checkpoint
    assert (workspace / "continued-checkpoint.json").is_file()
    assert json.loads((workspace / "b-closeout.json").read_text())["summary"]["terminal_complete"] is False
    assert json.loads((workspace / "b-ended.json").read_text())["summary"]["terminal_complete"] is False
    replayed = invoke(workspace, "--resume")
    assert replayed.returncode == 1 and "original checkpoint changed" in replayed.stderr
    commands = [json.loads(line) for line in (workspace / "commands.jsonl").read_text().splitlines()]
    assert any(row["returncode"] == 2 and "finish" in row["argv"] for row in commands)
    assert any(row["returncode"] == 1 and "write-guard" in row["argv"] for row in commands)


@pytest.mark.parametrize("symlink", [False, True])
def test_existing_workspace_and_symlink_are_preserved(tmp_path, symlink):
    target = tmp_path / "owner-directory"
    target.mkdir()
    note = target / "owner-note"
    note.write_bytes(b"keep unrelated work\x00")
    workspace = tmp_path / "example"
    if symlink:
        workspace.symlink_to(target, target_is_directory=True)
    else:
        workspace = target
    result = invoke(workspace)
    assert result.returncode == 2
    assert note.read_bytes() == b"keep unrelated work\x00" and list(target.iterdir()) == [note]


def test_resume_rejects_checkpoint_symlink_before_reading_or_logging(tmp_path):
    workspace = tmp_path / "example"
    workspace.mkdir()
    note = tmp_path / "owner-data"
    note.write_text("unrelated owner data")
    (workspace / "task-checkpoint.json").symlink_to(note)
    result = invoke(workspace, "--resume")
    assert result.returncode == 1 and "symlink example path" in result.stderr
    assert note.read_text() == "unrelated owner data" and not (workspace / "commands.jsonl").exists()
