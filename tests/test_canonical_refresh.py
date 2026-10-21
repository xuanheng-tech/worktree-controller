"""An ACTIVE idle canonical checkout follows published main by fast-forward only."""

from __future__ import annotations

import json
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from tests.test_controlled import _configure_remote, adopt_canonical
from tests.test_integration_pipeline import POLICY_THREE_CANDIDATES
from tests.test_shadow import GitRepository, git
from worktree_controller import canonical_refresh, writer_lease
from worktree_controller.canonical_refresh import refresh_canonical
from worktree_controller.cli import main as controller_main
from worktree_controller.git_backend import inventory
from worktree_controller.models import ControllerError
from worktree_controller.store import MetadataStore


@pytest.fixture
def published(tmp_path):
    with GitRepository(linked=0, policy_text=POLICY_THREE_CANDIDATES) as repository:
        adopt_canonical(repository)
        remote = _configure_remote(repository)
        before = git(repository.repo, "rev-parse", "HEAD")
        clone = tmp_path / "publisher"
        git(tmp_path, "clone", "-q", "-b", "main", str(remote), str(clone))
        (clone / "published.txt").write_text("published\n", encoding="utf-8")
        git(clone, "add", "published.txt")
        git(clone, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
            "commit", "-q", "-m", "publish")
        git(clone, "push", "-q", "origin", "HEAD:main")
        yield repository, before, git(clone, "rev-parse", "HEAD")


def _head(repository):
    return git(repository.repo, "rev-parse", "HEAD")


def test_behind_canonical_fast_forwards_to_published_main(published):
    repository, before, target = published
    decision = refresh_canonical(repository.repo)["decision"]
    assert decision["code"] == "CANONICAL_REFRESHED"
    assert (decision["before"], decision["target"]) == (before, target)
    assert _head(repository) == target
    assert git(repository.repo, "status", "--porcelain") == ""
    again = refresh_canonical(repository.repo)["decision"]
    assert again["code"] == "CANONICAL_CURRENT"


def test_dry_run_changes_nothing(published):
    repository, before, target = published
    decision = refresh_canonical(repository.repo, dry_run=True)["decision"]
    assert decision["code"] == "CANONICAL_REFRESH_READY" and decision["target"] == target
    assert _head(repository) == before


def test_dirty_canonical_is_refused(published):
    repository, before, _ = published
    (repository.repo / "unsaved.txt").write_text("work\n", encoding="utf-8")
    with pytest.raises(ControllerError) as refused:
        refresh_canonical(repository.repo)
    assert refused.value.code == "CANONICAL_DIRTY"
    assert _head(repository) == before


def test_diverged_canonical_is_refused(published):
    repository, _, _ = published
    (repository.repo / "local.txt").write_text("local\n", encoding="utf-8")
    git(repository.repo, "add", "local.txt")
    git(repository.repo, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
        "commit", "-q", "-m", "local only")
    local = _head(repository)
    with pytest.raises(ControllerError) as refused:
        refresh_canonical(repository.repo)
    assert refused.value.code == "NON_FAST_FORWARD"
    assert _head(repository) == local


def test_a_process_using_canonical_blocks_the_refresh(published):
    repository, before, _ = published
    user = subprocess.Popen(["sleep", "30"], cwd=repository.repo)
    try:
        with pytest.raises(ControllerError) as refused:
            refresh_canonical(repository.repo)
        assert refused.value.code == "CANONICAL_IN_USE" and str(user.pid) in str(refused.value)
    finally:
        user.terminate()
        user.wait(timeout=10)
    assert _head(repository) == before


def _fake_stat(root: Path, pid: int, name: str, parent: int) -> None:
    directory = root / str(pid)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "stat").write_text(f"{pid} ({name}) S {parent} 0 0\n")


def _occupancy(monkeypatch, used) -> None:
    monkeypatch.setattr(
        canonical_refresh.ProcessPathScan, "referenced_paths_in_use", lambda _self, _target: used
    )


def test_process_diagnostics_use_kernel_names_and_never_read_arguments(published, tmp_path, monkeypatch, capsys):
    repository, before, _ = published
    proc = tmp_path / "proc"
    _fake_stat(proc, 501, "node_repl", 401)
    _fake_stat(proc, 401, "codex", 1)
    (proc / "501/cmdline").write_text("fixture-private-argument")
    monkeypatch.setattr(canonical_refresh, "_PROC_ROOT", proc)
    monkeypatch.setattr(writer_lease, "_command", lambda _pid: pytest.fail("command arguments read"))
    _occupancy(monkeypatch, [(501, [str(repository.repo)])])

    assert controller_main(["canonical-refresh", "--repo", str(repository.repo), "--json"]) == 2
    output = capsys.readouterr().out
    payload = json.loads(output)
    assert payload["errors"][0]["code"] == "CANONICAL_IN_USE"
    diagnostic = payload["decision"]["process_diagnostics"]
    assert diagnostic["processes"] == [{
        "pid": 501, "name": "node_repl", "parent_pid": 401,
        "metadata_status": "observed", "parent_name": "codex",
        "matched_paths": [str(repository.repo)], "paths_truncated": False,
    }]
    assert "shared daemon" in " ".join(diagnostic["recovery_steps"])
    assert diagnostic["retry_argv"] == [
        "worktree-controller", "canonical-refresh", "--repo", str(repository.repo),
        "--dry-run", "--json",
    ]
    assert "fixture-private-argument" not in output
    assert _head(repository) == before


@pytest.mark.parametrize("failure", ["exited", "unreadable", "malformed", "oversized"])
def test_unknown_process_metadata_keeps_the_original_refusal(published, tmp_path, monkeypatch, failure):
    repository, before, _ = published
    proc = tmp_path / "proc"
    _fake_stat(proc, 501, "worker", 401)
    stat = proc / "501/stat"
    if failure == "exited":
        stat.unlink()
    elif failure == "malformed":
        stat.write_text("501 (worker) S invalid-parent\n")
    elif failure == "oversized":
        stat.write_text("501 (" + "x" * 5000 + ") S 401\n")
    else:
        original = Path.open

        def denied(path, *args, **kwargs):
            if path == stat:
                raise PermissionError("fixture denial")
            return original(path, *args, **kwargs)

        monkeypatch.setattr(Path, "open", denied)
    monkeypatch.setattr(canonical_refresh, "_PROC_ROOT", proc)
    _occupancy(monkeypatch, [(501, [str(repository.repo)])])
    with pytest.raises(ControllerError) as refused:
        refresh_canonical(repository.repo)
    assert refused.value.code == "CANONICAL_IN_USE"
    process = refused.value.diagnostics["processes"][0]
    assert process["metadata_status"] == process["name"] == process["parent_name"] == "unknown"
    assert process["parent_pid"] is None
    assert process["matched_paths"] == [str(repository.repo)]
    assert _head(repository) == before


def test_process_diagnostics_bound_processes_and_paths(published, tmp_path, monkeypatch, capsys):
    repository, before, _ = published
    monkeypatch.setattr(canonical_refresh, "_PROC_ROOT", tmp_path / "absent-proc")
    paths = [str(repository.repo / ("x" * 600 + str(index))) for index in range(5)]
    _occupancy(monkeypatch, [(pid, paths + [paths[0]]) for pid in reversed(range(501, 509))])
    assert controller_main(["canonical-refresh", "--repo", str(repository.repo), "--json"]) == 2
    diagnostic = json.loads(capsys.readouterr().out)["decision"]["process_diagnostics"]
    assert diagnostic["process_count"] == 8 and diagnostic["processes_omitted"] == 3
    assert [row["pid"] for row in diagnostic["processes"]] == list(range(501, 506))
    assert all(row["paths_truncated"] and len(row["matched_paths"]) == 3
               for row in diagnostic["processes"])
    assert all(len(path) <= 512 for row in diagnostic["processes"] for path in row["matched_paths"])
    assert _head(repository) == before


def test_text_refusal_shows_process_and_normal_session_recovery(published, tmp_path, monkeypatch, capsys):
    repository, before, _ = published
    proc = tmp_path / "proc"
    _fake_stat(proc, 501, "node\nworker", 401)
    _fake_stat(proc, 401, "codex", 1)
    monkeypatch.setattr(canonical_refresh, "_PROC_ROOT", proc)
    _occupancy(monkeypatch, [(501, [str(repository.repo)])])
    assert controller_main(["canonical-refresh", "--repo", str(repository.repo)]) == 2
    output = capsys.readouterr().out
    assert "pid=501 name='node\\nworker' parent_pid=401 parent_name='codex'" in output
    assert "shared daemon" in output and "normal lifecycle" in output
    assert "Retry after clearance:" in output and "--dry-run --json" in output
    assert _head(repository) == before


def test_frozen_canonical_keeps_the_transition_flow(published):
    repository, before, _ = published
    store = MetadataStore(inventory(repository.repo).common_dir)
    snapshot = store.load()
    record = next(r for r in snapshot.records.values() if r.role == "control-plane")
    with store.lock(exclusive=True):
        store.replace_record(replace(record, lifecycle="HANDOFF_READY"))
    with pytest.raises(ControllerError) as refused:
        refresh_canonical(repository.repo)
    assert refused.value.code == "CANONICAL_NOT_ACTIVE"
    assert "canonical-transition" in str(refused.value)
    assert _head(repository) == before


def test_cli_reports_refresh_and_refusal(published, capsys):
    repository, _, target = published
    assert controller_main(["canonical-refresh", "--repo", str(repository.repo), "--dry-run", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["decision"]["code"] == "CANONICAL_REFRESH_READY"
    (repository.repo / "unsaved.txt").write_text("work\n", encoding="utf-8")
    assert controller_main(["canonical-refresh", "--repo", str(repository.repo), "--json"]) == 2
    assert json.loads(capsys.readouterr().out)["errors"][0]["code"] == "CANONICAL_DIRTY"
    (repository.repo / "unsaved.txt").unlink()
    assert controller_main(["canonical-refresh", "--repo", str(repository.repo), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["decision"]["code"] == "CANONICAL_REFRESHED"
    assert _head(repository) == target
