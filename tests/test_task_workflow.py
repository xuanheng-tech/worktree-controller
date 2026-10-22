"""Real local repositories exercise the task facade and its refusal boundaries."""

from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from threading import Barrier

import pytest

from tests.test_cleanup_recovery import metadata_bytes
from tests.test_controlled import acquire_one, adopt_canonical
from tests.test_integration_abort import POLICY_THREE_CANDIDATES_V5
from tests.test_recovery_guidance import blocked as blocked_fixture
from tests.test_shadow import GitRepository, git
from worktree_controller import task_workflow as tasks
from worktree_controller.cli import main
from worktree_controller.controller import status
from worktree_controller.git_backend import inventory
from worktree_controller.lifecycle import complete_no_change, park, release
from worktree_controller.models import ControllerError
from worktree_controller.store import MetadataStore
from worktree_controller.writer_lease import acquire_writer_lease

blocked = blocked_fixture


@pytest.fixture
def repository():
    with GitRepository(linked=0, policy_text=POLICY_THREE_CANDIDATES_V5) as repository:
        git(repository.root, "init", "--bare", str(repository.root / "origin.git"))
        git(repository.repo, "remote", "add", "origin", str(repository.root / "origin.git"))
        git(repository.repo, "push", "-u", "origin", "main")
        adopt_canonical(repository)
        yield repository


def invoke(capsys, repository, *arguments, expected=0):
    result = main(["task", *arguments, "--repo", str(repository.repo), "--json"])
    captured = capsys.readouterr()
    assert result == expected, captured.out + captured.err
    return json.loads(captured.out)


def start(repository, name="demo", **options):
    result = tasks.start_task(repository.repo, task_key=name, owner="owner", scope_paths=("README.md",),
                              target=repository.root / name, **options)
    assert result["decision"]["code"] == "TASK_STARTED"
    return Path(result["decision"]["operation"]["path"])


def row(repository, path):
    return next(item for item in status(repository.repo)["worktrees"] if item["path"] == str(path))


def test_two_command_no_change_flow_and_read_only_history(repository, capsys):
    target = repository.root / "demo"
    started = invoke(capsys, repository, "start", "--task-key", "demo", "--owner", "owner",
                     "--scope", "README.md", "--path", str(target))
    assert started["decision"]["writer_entitlement_granted"] is False
    current = row(repository, target)
    assert current["authority_key"] == current["task_key"] == "demo"
    assert current["branch"] == "task/demo"
    assert current["scope_paths"] == ["README.md"]
    finished = invoke(capsys, repository, "finish", "--task-key", "demo", "--owner", "owner",
                      "--outcome", "no-change")
    assert finished["decision"]["code"] == "TASK_CHECKOUT_RELEASED"
    assert finished["decision"]["checkout_released"] is True
    assert not target.exists()
    assert git(repository.repo, "rev-parse", "task/demo") == current["head"]
    history = invoke(capsys, repository, "show", "--task-key", "demo")["decision"]["history"]
    assert history["closed_releases"][0]["proof_status"] == "COMPLETE"
    assert not history["incomplete_history"]
    repeated = invoke(capsys, repository, "finish", "--task-key", "demo", "--owner", "owner",
                      "--outcome", "no-change", expected=2)
    assert repeated["errors"][0]["code"] == "UNKNOWN_TASK"


def test_inspection_is_read_only_and_human_output_has_owner_next_step(repository, capsys):
    target = start(repository)
    store = MetadataStore(inventory(repository.repo).common_dir)
    before, refs = metadata_bytes(store), git(repository.repo, "show-ref")
    listing = invoke(capsys, repository, "list")["decision"]
    assert len(listing["tasks"]) == 1
    assert listing["tasks"][0]["writer"] == "ABSENT"
    shown = invoke(capsys, repository, "show", "--task-key", "demo")
    assert shown["decision"]["mutation_performed"] is False
    assert shown["decision"]["guidance"]["execution_authority"] is False
    assert metadata_bytes(store) == before and git(repository.repo, "show-ref") == refs
    assert main(["task", "show", "--repo", str(repository.repo), "--path", str(target)]) == 0
    output = capsys.readouterr().out
    assert "owner=owner" in output and "Next:" in output and str(target) in output


def test_checkpoint_pause_resume_preserves_unknown_file_and_original_identity(repository):
    target = start(repository)
    original = row(repository, target)
    unknown = target / "unknown-note.bin"
    unknown.write_bytes(b"fixture evidence\x00\xff")
    paused = tasks.finish_task(repository.repo, task_key="demo", owner="owner", outcome="checkpoint",
                               checkpoint_id="sha256:" + "a" * 64, checkpoint_head=original["head"],
                               waiting_on="external evidence")
    assert paused["decision"]["task"]["lifecycle"] == "RETAINED"
    assert paused["decision"]["checkout_released"] is False
    assert row(repository, target)["wait_checkpoint"]["waiting_on"] == "external evidence"
    continued = tasks.resume_task(repository.repo, task_key="demo", owner="owner")
    assert continued["decision"]["task"]["lifecycle"] == "ACTIVE"
    assert continued["decision"]["task"]["allocation_id"] == original["allocation_id"]
    assert continued["decision"]["writer_entitlement_granted"] is False
    assert unknown.read_bytes() == b"fixture evidence\x00\xff"
    with pytest.raises(ControllerError, match="clean worktree"):
        tasks.finish_task(repository.repo, task_key="demo", owner="owner", outcome="no-change")
    assert unknown.exists()


@pytest.mark.parametrize("outcome", ["no-change", "checkpoint", "handoff"])
@pytest.mark.parametrize("expired", [False, True])
def test_live_or_stale_recorded_writer_is_not_implicitly_released(repository, outcome, expired):
    target = start(repository)
    current = row(repository, target)
    lease = acquire_writer_lease(repository.repo, target=target, owner="owner", client_id="fixture",
                                  holder_pid=os.getpid(), session_id="fixture")["decision"]
    store = MetadataStore(inventory(repository.repo).common_dir)
    if expired:
        with store.lock(exclusive=True):
            recorded = store._load_unlocked().writer_leases[current["allocation_id"]]
            store.write_writer_lease(replace(recorded, expires_at="2000-01-01T00:00:00+00:00"))
    options = {"checkpoint_id": "sha256:" + "b" * 64, "checkpoint_head": current["head"]} if outcome == "checkpoint" else (
        {"finalizer_record_id": "unit-fixture-only", "commit": current["head"]} if outcome == "handoff" else {})
    with pytest.raises(ControllerError) as failure:
        tasks.finish_task(repository.repo, task_key="demo", owner="owner", outcome=outcome, **options)
    assert failure.value.code == "WRITER_LEASE_PRESENT"
    snapshot = MetadataStore(inventory(repository.repo).common_dir).load()
    assert snapshot.writer_leases[current["allocation_id"]].lease_id == lease["lease_id"]
    assert row(repository, target)["lifecycle"] == "ACTIVE"


def test_no_change_release_failure_reports_completed_prefix_and_retries_only_release(repository, capsys):
    target = start(repository)
    exclude = repository.repo / ".git/info/exclude"
    with exclude.open("a") as stream:
        stream.write("\ncache/\n")
    (target / "cache").mkdir()
    artifact = target / "cache/unknown.bin"
    artifact.write_bytes(b"keep unknown fixture")
    first = invoke(capsys, repository, "finish", "--task-key", "demo", "--owner", "owner",
                   "--outcome", "no-change", expected=2)
    assert first["decision"]["code"] == "TASK_RELEASE_BLOCKED"
    assert len(first["decision"]["operations"]) == 1
    assert first["decision"]["operations"][0]["code"] == "NO_CHANGE_COMPLETED"
    assert first["decision"]["operation_complete"] is False
    guidance = tasks.inspect_task(repository.repo, task_key="demo")["decision"]["guidance"]
    assert guidance["next_steps"][0]["action"] == "inspect_release"
    assert artifact.read_bytes() == b"keep unknown fixture"
    version = row(repository, target)["record_version"]
    again = invoke(capsys, repository, "finish", "--task-key", "demo", "--owner", "owner",
                   "--outcome", "no-change", expected=2)
    assert again["decision"]["operations"] == []
    assert row(repository, target)["record_version"] == version
    assert artifact.exists()


def test_owner_wrong_head_contradictory_evidence_and_duplicate_start_refuse(repository):
    target = start(repository)
    store = MetadataStore(inventory(repository.repo).common_dir)
    before = metadata_bytes(store)
    requests = [
        lambda: tasks.resume_task(repository.repo, task_key="demo", owner="other"),
        lambda: tasks.finish_task(repository.repo, task_key="demo", owner="owner", outcome="checkpoint",
                                  checkpoint_id="sha256:" + "a" * 64, checkpoint_head="f" * 40),
        lambda: tasks.finish_task(repository.repo, task_key="demo", owner="owner", outcome="no-change",
                                  checkpoint_id="sha256:" + "a" * 64),
        lambda: start(repository),
        lambda: tasks.inspect_task(repository.repo, task_key="demo", target=target),
    ]
    for request in requests:
        with pytest.raises(ControllerError):
            request()
        assert metadata_bytes(store) == before


def test_default_base_is_never_silently_substituted_and_explicit_local_base_works(repository):
    git(repository.repo, "remote", "remove", "origin")
    with pytest.raises(ControllerError) as failure:
        start(repository)
    assert failure.value.code == "TASK_BASE_UNAVAILABLE"
    assert not (repository.root / "demo").exists()
    assert start(repository, base_ref="main").is_dir()


def test_resource_wait_is_not_successful_start(repository, capsys, monkeypatch):
    def wait(*_arguments, **_options):
        payload = status(repository.repo)
        payload["decision"] = {"code": "RESOURCE_WAIT", "request_id": "unit-fixture-only"}
        return payload

    monkeypatch.setattr(tasks, "acquire", wait)
    result = invoke(capsys, repository, "start", "--task-key", "waiting", "--owner", "owner",
                    "--scope", "README.md", expected=2)
    assert result["decision"]["code"] == "TASK_WAITING"
    assert result["decision"]["operation_complete"] is False


@pytest.mark.parametrize("drift", ["head", "scope"])
def test_handoff_identity_and_scope_drift_still_refuse(repository, drift):
    target = start(repository)
    old_head = git(target, "rev-parse", "HEAD")
    (target / ("README.md" if drift == "head" else "outside.txt")).write_text("fixture change\n")
    git(target, "add", ".")
    git(target, "commit", "-m", "fixture drift")
    head = git(target, "rev-parse", "HEAD")
    with pytest.raises(ControllerError):
        tasks.finish_task(repository.repo, task_key="demo", owner="owner", outcome="handoff",
                          finalizer_record_id="unit-fixture-only", commit=old_head if drift == "head" else head)
    assert row(repository, target)["scope_paths"] == ["README.md"]
    assert row(repository, target)["handoff_commit"] is None
    assert git(target, "rev-parse", "HEAD") == head


def test_handoff_keeps_exact_scope_and_commit_and_remains_pending_integration(repository):
    target = start(repository)
    (target / "README.md").write_text("unit fixture change\n")
    git(target, "add", "README.md")
    git(target, "commit", "-m", "fixture change")
    head = git(target, "rev-parse", "HEAD")
    result = tasks.finish_task(repository.repo, task_key="demo", owner="owner", outcome="handoff",
                               finalizer_record_id="unit-fixture-only", commit=head)
    decision = result["decision"]
    assert decision["code"] == "TASK_HANDED_OFF"
    assert decision["business_task_completed"] is False
    assert decision["checkout_released"] is False
    current = row(repository, target)
    assert current["scope_paths"] == ["README.md"]
    assert current["handoff_commit"] == current["finalizer_commit"] == head
    assert current["lifecycle"] == "HANDOFF_READY"


def test_ambiguous_task_requires_explicit_path_and_never_chooses_an_owner(repository):
    target = start(repository)
    acquire_one(repository, task="duplicate", authority="other-authority", owner="other")
    # Duplicate task keys are legal imported metadata; fixture adoption retains
    # both allocations so the selector must not pick by order or caller owner.
    store = MetadataStore(inventory(repository.repo).common_dir)
    with store.lock(exclusive=True):
        current = store._load_unlocked()
        duplicate = next(record for record in current.records.values() if record.task_key == "duplicate")
        store.replace_record(replace(duplicate, task_key="demo"))
    with pytest.raises(ControllerError) as failure:
        tasks.resume_task(repository.repo, task_key="demo", owner="owner")
    assert failure.value.code == "AMBIGUOUS_TASK"
    assert tasks.inspect_task(repository.repo, target=target)["decision"]["guidance"]["identity"]["owner"] == "owner"


def test_concurrent_resume_has_one_success_and_one_cas_refusal(repository, monkeypatch):
    target = start(repository)
    park(repository.repo, target=target, owner="owner", expected_record_version=1)
    barrier = Barrier(2)
    real_resume = tasks.resume

    def synchronized(*args, **kwargs):
        barrier.wait(timeout=10)
        return real_resume(*args, **kwargs)

    monkeypatch.setattr(tasks, "resume", synchronized)

    def request():
        try:
            return tasks.resume_task(repository.repo, task_key="demo", owner="owner")["decision"]["code"]
        except ControllerError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: request(), range(2)))
    assert sorted(results) == ["CAS_MISMATCH", "TASK_RESUMED"]
    assert row(repository, target)["record_version"] == 3


def test_replaced_checkout_with_same_owner_path_and_cas_cannot_receive_old_resume(repository, monkeypatch):
    target = start(repository)
    park(repository.repo, target=target, owner="owner", expected_record_version=1)
    old = row(repository, target)
    real_resume = tasks.resume

    def replace_before_mutation(*args, **kwargs):
        completed = complete_no_change(repository.repo, target=target, owner="owner", expected_record_version=2)
        release(repository.repo, target=target, owner_release="owner",
                expected_record_version=completed["decision"]["record_version"])
        tasks.start_task(repository.repo, task_key="replacement", owner="owner", scope_paths=("README.md",),
                         target=target)
        park(repository.repo, target=target, owner="owner", expected_record_version=1)
        return real_resume(*args, **kwargs)

    monkeypatch.setattr(tasks, "resume", replace_before_mutation)
    with pytest.raises(ControllerError) as failure:
        tasks.resume_task(repository.repo, task_key="demo", owner="owner")
    assert failure.value.code == "IDENTITY_MISMATCH"
    current = row(repository, target)
    assert current["record_version"] == old["record_version"] == 2
    assert current["allocation_id"] != old["allocation_id"]
    assert current["lifecycle"] == "RETAINED"


def test_replacement_between_completion_and_release_is_preserved(repository, monkeypatch):
    target = start(repository)
    original = row(repository, target)
    real_release = tasks.release

    def replace_before_release(*args, **kwargs):
        real_release(*args, **kwargs)
        tasks.start_task(repository.repo, task_key="replacement", owner="owner", scope_paths=("README.md",),
                         target=target)
        complete_no_change(repository.repo, target=target, owner="owner", expected_record_version=1)
        return real_release(*args, **kwargs)

    monkeypatch.setattr(tasks, "release", replace_before_release)
    result = tasks.finish_task(repository.repo, task_key="demo", owner="owner", outcome="no-change")
    assert result["decision"]["code"] == "TASK_RELEASE_BLOCKED"
    assert len(result["decision"]["operations"]) == 1
    assert result["errors"][0]["code"] == "IDENTITY_MISMATCH"
    recovery = result["decision"]["next_steps"][0]["argv"]
    assert recovery[1] == "lifecycle-history"
    assert original["allocation_id"] in recovery and "--path" not in recovery
    current = row(repository, target)
    assert current["allocation_id"] != original["allocation_id"]
    assert current["lifecycle"] == "NO_CHANGE_COMPLETED"
    assert target.is_dir()


def test_real_blocked_task_recovers_through_existing_commands_only(blocked, capsys):
    repository, candidate, claimed, _source = blocked
    current = row(repository, candidate)
    store = MetadataStore(inventory(repository.repo).common_dir)
    before = metadata_bytes(store)
    guide = invoke(capsys, repository, "recover", "--task-key", current["task_key"])["decision"]["guidance"]
    assert metadata_bytes(store) == before
    assert guide["identity"]["lifecycle"] == "BLOCKED"
    assert guide["blockers"] == []
    assert guide["next_steps"][0]["action"] == "quarantine"
    arguments = guide["next_steps"][0]["argv"]
    assert main(arguments[1:]) == 0
    capsys.readouterr()
    next_guide = invoke(capsys, repository, "recover", "--intent-id", claimed["intent_id"])["decision"]["guidance"]
    assert next_guide["identity"]["lifecycle"] == "QUARANTINED"
    assert next_guide["next_steps"][0]["action"] == "abort"
