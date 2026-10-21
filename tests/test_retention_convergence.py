"""Idle clean checkouts leave through guarded release; nothing else is touched."""

from __future__ import annotations

import time

import pytest

from tests.test_controlled import (
    _commit_reproducible_project,
    _create_reproducible_venv,
    acquire_one,
    adopt_canonical,
)
from tests.test_retained_archive import _abandoned_source
from tests.test_retention_admission import POLICY_V5
from tests.test_shadow import GitRepository, git
from worktree_controller import convergence, lifecycle
from worktree_controller.controller import doctor
from worktree_controller.convergence import (
    admission_convergence,
    converge_retention,
    idle_threshold_hours,
)
from worktree_controller.delivered_closeout import close_delivered
from worktree_controller.models import ControllerError, ConvergencePolicy
from worktree_controller.policy import load_policy
from worktree_controller.store import MetadataStore
from worktree_controller.writer_lease import acquire_writer_lease

ACTOR = "lifecycle-reconciler"
FUTURE = time.time() + 30 * 24 * 3600

POLICY_V8 = (
    POLICY_V5.replace("schema_version = 5", "schema_version = 8")
    + """
[reconciler]
enabled = true
actor = "lifecycle-reconciler"
allow_legacy_adoption = false

[admission]
strategy = "resource-budget-v1"
checkout_growth_bytes = 2147483648
checkout_growth_inodes = 100000

[convergence]
enabled = true
grace_minutes = 30
idle_hours_max = 72
idle_hours_min = 6
standing_reserve = 2
"""
)


@pytest.fixture
def repo(monkeypatch):
    evidence = {
        "managed_references": [],
        "observed_references": [],
        "managed_visibility_uncertainties": [],
        "scan_uncertainties": [],
    }
    monkeypatch.setattr(lifecycle, "_typed_runtime_evidence", lambda *_args: evidence)
    monkeypatch.setattr(lifecycle, "_validate_runtime_clear", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(convergence, "busy_paths", lambda _targets: set())
    with GitRepository(linked=0, policy_text=POLICY_V8) as repository:
        _commit_reproducible_project(repository)
        adopt_canonical(repository)
        remote = repository.root / "published.git"
        git(repository.root, "init", "--bare", str(remote))
        git(repository.repo, "remote", "add", "origin", str(remote))
        git(repository.repo, "push", "origin", "main")
        yield repository


def _retained(repository, task, *, publish, venv=False, role="feature"):
    path, allocation = acquire_one(repository, task=task, authority=f"{task}-authority", role=role)
    (path / f"{task}.txt").write_text("synthetic result\n")
    git(path, "add", f"{task}.txt")
    git(path, "commit", "-m", f"synthetic {task}")
    if publish:
        git(path, "push", "origin", "HEAD:main")
        git(repository.repo, "fetch", "origin", "main")
    if venv:
        _create_reproducible_venv(path)
    lifecycle.park(repository.repo, target=path, expected_record_version=1, owner="module-owner")
    return path, allocation["decision"]["allocation_id"]


def _row(payload, path):
    return next(row for row in payload["decision"]["findings"] if row["path"] == str(path))


def test_published_checkout_with_venv_is_closed_and_released(repo):
    path, allocation_id = _retained(repo, "delivered", publish=True, venv=True)
    branch_head = git(path, "rev-parse", "HEAD")
    preview = converge_retention(repo.repo, actor=ACTOR, now_at=FUTURE)
    assert _row(preview, path)["outcome"] == "WOULD_RELEASE"
    assert path.exists()
    applied = converge_retention(repo.repo, actor=ACTOR, dry_run=False, now_at=FUTURE)
    row = _row(applied, path)
    assert row["outcome"] == "RELEASED", row
    assert row["route"] == "close-delivered"
    assert not path.exists()
    assert git(repo.repo, "rev-parse", "feature/delivered") == branch_head
    snapshot = MetadataStore(repo.repo / ".git").load()
    record = snapshot.records[allocation_id]
    assert record.lifecycle == "RELEASED"
    receipt = snapshot.release_receipts[row["receipt_id"]]
    assert receipt.allocation_id == allocation_id
    assert receipt.released_head == branch_head
    assert receipt.branch == "feature/delivered"
    assert receipt.plan_id == row["release_plan_id"]
    assert applied["decision"]["reason"].startswith("1 idle checkout(s) closed")
    assert doctor(repo.repo)["decision"]["code"] == "PASS"


def test_unfinished_checkout_waits_then_is_archived_on_its_branch(repo):
    path, _ = _retained(repo, "unfinished", publish=False)
    head = git(path, "rev-parse", "HEAD")
    now = converge_retention(repo.repo, actor=ACTOR, dry_run=False)
    assert _row(now, path)["outcome"] == "IDLE_BELOW_THRESHOLD"
    assert path.exists()
    later = converge_retention(repo.repo, actor=ACTOR, dry_run=False, now_at=FUTURE)
    row = _row(later, path)
    assert row["outcome"] == "RELEASED", row
    assert row["route"] == "archive-retained"
    assert git(repo.repo, "rev-parse", "feature/unfinished") == head


@pytest.mark.parametrize("publish", [False, True])
def test_formally_abandoned_source_converges_through_its_normal_closeout(repo, publish):
    path, allocation_id = _retained(repo, "abandoned-source", publish=publish)
    store, intent_id = _abandoned_source(repo, path, allocation_id, "abandoned-source.txt")
    intent_path = store.integration_intents_dir / f"{intent_id}.json"
    intent_bytes = intent_path.read_bytes()
    head = git(path, "rev-parse", "HEAD")
    preview = converge_retention(repo.repo, actor=ACTOR, now_at=FUTURE)
    row = _row(preview, path)
    assert row["outcome"] == "WOULD_RELEASE", row
    assert row["route"] == ("close-delivered" if publish else "archive-retained")
    applied = converge_retention(repo.repo, actor=ACTOR, dry_run=False, now_at=FUTURE)
    assert _row(applied, path)["outcome"] == "RELEASED", _row(applied, path)
    assert intent_path.read_bytes() == intent_bytes
    assert git(repo.repo, "rev-parse", "feature/abandoned-source") == head
    assert store.load().records[allocation_id].lifecycle == "RELEASED"


def test_dirty_live_and_active_checkouts_are_never_touched(repo):
    dirty, _ = _retained(repo, "dirty", publish=True)
    (dirty / "draft.txt").write_text("uncommitted\n")
    leased, leased_id = _retained(repo, "leased", publish=False)
    lifecycle.resume(repo.repo, target=leased, expected_record_version=2, owner="module-owner")
    acquire_writer_lease(
        repo.repo,
        target=leased,
        owner="module-owner",
        client_id="test",
        holder_pid=__import__("os").getpid(),
        session_id="session",
        run_id="run",
    )
    result = converge_retention(repo.repo, actor=ACTOR, dry_run=False, now_at=FUTURE)
    assert _row(result, dirty)["outcome"] == "ATTENTION_REQUIRED"
    assert _row(result, leased)["outcome"] == "IN_USE"
    assert dirty.exists() and leased.exists()
    assert (dirty / "draft.txt").read_text() == "uncommitted\n"
    assert result["decision"]["attention_required"] == 1
    assert result["decision"]["live"] == 1
    assert result["decision"]["outcome_counts"] == {"ATTENTION_REQUIRED": 1, "IN_USE": 1}
    reason = result["decision"]["reason"]
    assert reason == "no checkout was released; 1 in use, 1 need owner action"
    dirty_row = _row(result, dirty)
    assert dirty_row["changes"] == {"staged": 0, "modified": 0, "untracked": 1}
    assert "module-owner" in dirty_row["owner_action"]
    action = dirty_row["owner_action_request"]
    assert action["allocation_id"] == dirty_row["allocation_id"]
    assert action["owner"] == "module-owner" and action["expected_record_version"] == 2
    assert action["execution_authority"] is False and action["automatic_discard"] is False
    del leased_id


def test_unpublished_handoff_is_held_and_published_handoff_is_released(repo):
    path, allocation = acquire_one(repo, task="handoff", authority="handoff-authority")
    (path / "handoff.txt").write_text("handoff\n")
    git(path, "add", "handoff.txt")
    git(path, "commit", "-m", "handoff")
    head = git(path, "rev-parse", "HEAD")
    lifecycle.handoff(
        repo.repo,
        target=path,
        expected_record_version=1,
        kind="integration-ready",
        commit=head,
        finalizer_record_id="finalizer:test",
    )
    _create_reproducible_venv(path)
    held = converge_retention(repo.repo, actor=ACTOR, dry_run=False, now_at=FUTURE)
    assert _row(held, path)["outcome"] == "BLOCKED"
    assert path.exists()
    git(path, "push", "origin", "HEAD:main")
    git(repo.repo, "fetch", "origin", "main")
    released = converge_retention(repo.repo, actor=ACTOR, dry_run=False, now_at=FUTURE)
    assert _row(released, path)["outcome"] == "RELEASED", _row(released, path)
    assert not path.exists()
    assert git(repo.repo, "rev-parse", "feature/handoff") == head
    assert doctor(repo.repo)["decision"]["code"] == "PASS"
    del allocation


def test_apply_requires_delegated_actor(repo):
    with pytest.raises(ControllerError) as mismatch:
        converge_retention(repo.repo, actor="someone-else", dry_run=False)
    assert mismatch.value.code == "RECONCILER_ACTOR_MISMATCH"
    assert converge_retention(repo.repo, actor="someone-else")["decision"]["dry_run"] is True


def test_threshold_shrinks_with_population_pressure():
    settings = ConvergencePolicy(
        enabled=True, grace_minutes=30, idle_hours_max=72, idle_hours_min=6, standing_reserve=2
    )
    assert idle_threshold_hours(settings, registered=4, live=2) == 72
    assert idle_threshold_hours(settings, registered=8, live=2) == 72 / 4
    assert idle_threshold_hours(settings, registered=40, live=2) == 6


def test_close_delivered_accepts_registered_reproducible_venv(repo):
    path, _ = _retained(repo, "venv-closeout", publish=True, venv=True)
    lifecycle.register_disposable(
        repo.repo,
        target=path,
        expected_record_version=2,
        owner="module-owner",
        resource_path=".venv",
        kind="python-venv",
        reproducible=True,
        creator="uv",
        teardown_policy="on-release",
    )
    args = dict(target=path, expected_record_version=3, owner_release="module-owner", actor="gov")
    planned = close_delivered(repo.repo, **args, dry_run=True)
    closed = close_delivered(repo.repo, **args, plan_id=planned["decision"]["plan_id"])
    assert closed["decision"]["code"] == "CHECKOUT_CLOSED"
    released = lifecycle.release(
        repo.repo,
        target=path,
        expected_record_version=4,
        owner_release="module-owner",
        teardown_reproducible_venv=True,
    )
    assert released["decision"]["code"] == "RELEASED"
    assert not path.exists()


def test_admission_convergence_is_detached_and_never_raises(repo, monkeypatch):
    started = {}

    class FakeChild:
        pid = 4242

    def fake_spawn(arguments):
        started.update(arguments=arguments)
        return FakeChild()

    monkeypatch.setattr(convergence, "_spawn_detached", fake_spawn)
    scheduled = admission_convergence(repo.repo)
    assert scheduled["code"] == "CONVERGENCE_SCHEDULED"
    assert scheduled["pid"] == 4242
    assert "last_applied_pass" in scheduled["result"]
    arguments = started["arguments"]
    assert arguments[1:5] == ["-m", "worktree_controller.cli", "reconcile", "retention"]
    assert arguments[arguments.index("--trigger") + 1] == "admission"
    assert "--apply" in arguments

    def broken(*_args, **_kwargs):
        raise OSError("synthetic spawn failure")

    monkeypatch.setattr(convergence, "_spawn_detached", broken)
    assert admission_convergence(repo.repo)["code"] == "CONVERGENCE_SKIPPED"


def test_concurrent_apply_pass_reports_busy(repo):
    import fcntl

    lock_path = convergence._pass_lock_path(repo.repo / ".git")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        busy = converge_retention(repo.repo, actor=ACTOR, dry_run=False)
    assert busy["decision"]["code"] == "CONVERGENCE_BUSY"
    assert converge_retention(repo.repo, actor=ACTOR, dry_run=False)["decision"]["code"] in {
        "NO_RETENTION_ACTION",
        "RETENTION_CONVERGED",
    }


def test_policy_v8_requires_reconciler_and_ordered_thresholds(repo):
    policy_path = repo.repo / ".agents" / "worktree-policy.toml"
    assert load_policy(repo.repo).convergence.standing_reserve == 2
    policy_path.write_text(POLICY_V8.replace("idle_hours_min = 6", "idle_hours_min = 99"))
    with pytest.raises(ControllerError):
        load_policy(repo.repo)
    policy_path.write_text(POLICY_V8.replace("enabled = true\nactor", "enabled = false\nactor"))
    with pytest.raises(ControllerError):
        load_policy(repo.repo)


def test_dormant_active_checkout_is_parked_only_after_the_idle_threshold(repo):
    path, allocation = acquire_one(repo, task="dormant", authority="dormant-authority")
    (path / "dormant.txt").write_text("committed by a client without writer leases\n")
    git(path, "add", "dormant.txt")
    git(path, "commit", "-m", "dormant work")
    head = git(path, "rev-parse", "HEAD")
    fresh = converge_retention(repo.repo, actor=ACTOR, dry_run=False)
    assert _row(fresh, path)["outcome"] == "IDLE_BELOW_THRESHOLD"
    assert fresh["decision"]["live"] == 0
    later = converge_retention(repo.repo, actor=ACTOR, dry_run=False, now_at=FUTURE)
    row = _row(later, path)
    assert row["outcome"] == "RELEASED", row
    assert row["parked"] is True
    assert row["route"] == "archive-retained"
    assert git(repo.repo, "rev-parse", "feature/dormant") == head
    record = (
        MetadataStore(repo.repo / ".git").load().records[allocation["decision"]["allocation_id"]]
    )
    assert [event["event"] for event in record.history][-3:-1] == [
        "PARKED",
        "RETAINED_CHECKOUT_ARCHIVED",
    ]


def test_busy_paths_matches_the_per_checkout_scan(tmp_path):
    inside = tmp_path / "checkout"
    inside.mkdir()
    other = tmp_path / "other"
    other.mkdir()
    import subprocess
    import sys

    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], cwd=inside)
    try:
        found = convergence.busy_paths({str(inside), str(other)})
    finally:
        child.kill()
        child.wait()
    assert found == {str(inside)}


def test_pressure_never_shortens_the_dormant_active_threshold(repo):
    active, _ = acquire_one(repo, task="quiet-lane", authority="quiet-authority")
    retained = []
    for index in range(6):
        path, _ = _retained(repo, f"parked-{index}", publish=False)
        retained.append(path)
    # 7 registered, 0 live, reserve 2: pressure 3.5 shrinks the threshold to its 6h floor.
    later = time.time() + 24 * 3600
    result = converge_retention(repo.repo, actor=ACTOR, now_at=later)
    assert result["decision"]["idle_threshold_hours"] == 6
    row = _row(result, active)
    assert row["outcome"] == "IDLE_BELOW_THRESHOLD", row
    assert row["required_idle_hours"] == 72
    assert all(_row(result, path)["outcome"] == "WOULD_RELEASE" for path in retained)


def _enable_unintegrated_release(repository):
    policy_path = repository.repo / ".agents" / "worktree-policy.toml"
    policy_path.write_text(POLICY_V8 + "release_unintegrated_handoffs = true\n")
    assert load_policy(repository.repo).convergence.release_unintegrated_handoffs is True


def _frozen_handoff(repository, task, *, role="feature"):
    path, allocation = acquire_one(
        repository,
        task=task,
        authority=f"{task}-authority",
        role=role,
        scope_paths=(f"{task}.txt",),
    )
    (path / f"{task}.txt").write_text("frozen synthetic result\n")
    git(path, "add", f"{task}.txt")
    git(path, "commit", "-m", f"synthetic {task}")
    head = git(path, "rev-parse", "HEAD")
    lifecycle.handoff(
        repository.repo,
        target=path,
        expected_record_version=1,
        kind="integration-ready",
        commit=head,
        finalizer_record_id=f"finalizer:{task}",
    )
    return path, allocation["decision"]["allocation_id"], head


def test_unintegrated_handoff_is_held_with_its_reason_unless_policy_opts_in(repo):
    path, _, _ = _frozen_handoff(repo, "held-handoff")
    held = converge_retention(repo.repo, actor=ACTOR, dry_run=False, now_at=FUTURE)
    row = _row(held, path)
    assert row["outcome"] == "BLOCKED"
    assert "not integrated" in row["reason"]
    assert "1 blocked" in held["decision"]["reason"]
    assert path.exists()


def test_unintegrated_handoff_waits_for_its_remote_branch_then_releases_and_replays(repo):
    _enable_unintegrated_release(repo)
    path, allocation_id, head = _frozen_handoff(repo, "early-handoff")
    unpushed = converge_retention(repo.repo, actor=ACTOR, dry_run=False, now_at=FUTURE)
    assert _row(unpushed, path)["outcome"] == "BLOCKED"
    assert "origin/feature/early-handoff" in _row(unpushed, path)["reason"]
    git(path, "push", "origin", "feature/early-handoff")
    git(repo.repo, "fetch", "origin")
    fresh = converge_retention(repo.repo, actor=ACTOR, dry_run=False)
    assert _row(fresh, path)["outcome"] == "IDLE_BELOW_THRESHOLD"
    released = converge_retention(repo.repo, actor=ACTOR, dry_run=False, now_at=FUTURE)
    row = _row(released, path)
    assert row["outcome"] == "RELEASED", row
    assert row["route"] == "release-retained-handoff"
    assert not path.exists()
    assert git(repo.repo, "rev-parse", "feature/early-handoff") == head
    store = MetadataStore(repo.repo / ".git")
    assert store.load().records[allocation_id].handoff_commit == head

    queued = lifecycle.enqueue_integration(
        repo.repo,
        task_key="early-handoff-integration",
        authority_key="early-handoff-integration",
        module="integration",
        owner="integration-owner",
        source_handoffs=(head,),
    )
    intent = store.load().integration_intents[queued["decision"]["intent_id"]]
    assert intent.source_allocations == (allocation_id,)
    assert intent.source_handoffs == (head,)
    assert intent.source_branches == ("feature/early-handoff",)


def test_released_source_replays_only_while_its_branch_retains_the_handoff(repo):
    _enable_unintegrated_release(repo)
    path, allocation_id, head = _frozen_handoff(repo, "moved-handoff")
    git(path, "push", "origin", "feature/moved-handoff")
    git(repo.repo, "fetch", "origin")
    released = converge_retention(repo.repo, actor=ACTOR, dry_run=False, now_at=FUTURE)
    assert _row(released, path)["outcome"] == "RELEASED"
    git(repo.repo, "branch", "-f", "feature/moved-handoff", "main")
    with pytest.raises(ControllerError) as moved:
        lifecycle.enqueue_integration(
            repo.repo,
            task_key="moved-handoff-integration",
            authority_key="moved-handoff-integration",
            module="integration",
            owner="integration-owner",
            source_allocations=(allocation_id,),
        )
    assert moved.value.code == "SOURCE_BRANCH_UNAVAILABLE"
    del head


def test_queued_unintegrated_handoff_keeps_its_checkout(repo):
    _enable_unintegrated_release(repo)
    path, allocation_id, _ = _frozen_handoff(repo, "queued-handoff")
    git(path, "push", "origin", "feature/queued-handoff")
    git(repo.repo, "fetch", "origin")
    lifecycle.enqueue_integration(
        repo.repo,
        task_key="queued-handoff-integration",
        authority_key="queued-handoff-integration",
        module="integration",
        owner="integration-owner",
        source_allocations=(allocation_id,),
    )
    result = converge_retention(repo.repo, actor=ACTOR, dry_run=False, now_at=FUTURE)
    assert _row(result, path)["outcome"] == "BLOCKED"
    assert _row(result, path)["reason"] == "integration lineage still references this checkout"
    assert path.exists()


def test_published_hotfix_handoff_is_released_and_an_open_one_is_held(repo):
    plain, _, plain_head = _frozen_handoff(repo, "plain-hotfix", role="hotfix")
    git(plain, "push", "origin", "HEAD:main")
    git(repo.repo, "fetch", "origin", "main")
    # A hotfix that is not yet published keeps its own flow.
    unpublished, _, _ = _frozen_handoff(repo, "open-hotfix", role="hotfix")
    result = converge_retention(repo.repo, actor=ACTOR, dry_run=False, now_at=FUTURE)
    row = _row(result, plain)
    assert row["outcome"] == "RELEASED", row
    assert row["route"] == "release-published-handoff"
    assert git(repo.repo, "rev-parse", "feature/plain-hotfix") == plain_head
    assert _row(result, unpublished)["outcome"] == "BLOCKED"
    assert "not integrated" in _row(result, unpublished)["reason"]


def test_pushed_hotfix_handoff_leaves_before_integration_like_any_handoff(repo):
    _enable_unintegrated_release(repo)
    path, _, head = _frozen_handoff(repo, "pushed-hotfix", role="hotfix")
    git(path, "push", "origin", "feature/pushed-hotfix")
    git(repo.repo, "fetch", "origin")
    released = converge_retention(repo.repo, actor=ACTOR, dry_run=False, now_at=FUTURE)
    row = _row(released, path)
    assert row["outcome"] == "RELEASED", row
    assert row["route"] == "release-retained-handoff"
    assert git(repo.repo, "rev-parse", "feature/pushed-hotfix") == head


def test_published_hotfix_with_unregistered_venv_is_blocked_for_its_owner(repo):
    path, _, _ = _frozen_handoff(repo, "venv-hotfix", role="hotfix")
    git(path, "push", "origin", "HEAD:main")
    git(repo.repo, "fetch", "origin", "main")
    _create_reproducible_venv(path)
    result = converge_retention(repo.repo, actor=ACTOR, dry_run=False, now_at=FUTURE)
    row = _row(result, path)
    assert row["outcome"] == "BLOCKED", row
    assert row["code"] == "RESOURCE_UNCLASSIFIED"
    assert "handoff-owner-attestation:module-owner" in row["reason"]
    assert path.exists()


def test_applied_pass_report_is_visible_to_the_next_dry_run(repo):
    assert converge_retention(repo.repo, actor=ACTOR)["decision"]["last_applied_pass"] is None
    path, allocation_id = _retained(repo, "reported", publish=True)
    applied = converge_retention(
        repo.repo, actor=ACTOR, trigger="admission", dry_run=False, now_at=FUTURE
    )
    receipt_id = _row(applied, path)["receipt_id"]
    report = converge_retention(repo.repo, actor=ACTOR)["decision"]["last_applied_pass"]
    assert report["state"] == "COMPLETED"
    assert report["trigger"] == "admission"
    assert report["code"] == "RETENTION_CONVERGED"
    assert report["released"] == [
        {"allocation_id": allocation_id, "receipt_id": receipt_id, "path": str(path)}
    ]
    assert report["started_at"] <= report["finished_at"]


def test_failed_and_interrupted_passes_are_not_reported_as_success(repo, monkeypatch):
    def failing(*_args, **_kwargs):
        raise ControllerError("SYNTHETIC_FAILURE", "synthetic pass failure")

    monkeypatch.setattr(convergence, "_converge", failing)
    with pytest.raises(ControllerError):
        converge_retention(repo.repo, actor=ACTOR, dry_run=False)
    common_dir = repo.repo / ".git"
    failed = convergence.last_applied_pass(common_dir)
    assert failed["state"] == "FAILED"
    assert failed["code"] == "SYNTHETIC_FAILURE"
    lock_path = convergence._pass_lock_path(common_dir)
    convergence._write_pass_report(lock_path, {"state": "RUNNING", "pid": 1})
    assert convergence.last_applied_pass(common_dir)["state"] == "INTERRUPTED"
    import fcntl

    with lock_path.open("a+b") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert convergence.last_applied_pass(common_dir)["state"] == "RUNNING"


def test_policy_release_unintegrated_handoffs_is_optional_and_boolean(repo):
    policy_path = repo.repo / ".agents" / "worktree-policy.toml"
    assert load_policy(repo.repo).convergence.release_unintegrated_handoffs is False
    policy_path.write_text(POLICY_V8 + 'release_unintegrated_handoffs = "yes"\n')
    with pytest.raises(ControllerError):
        load_policy(repo.repo)
    policy_path.write_text(POLICY_V8 + "unknown_switch = true\n")
    with pytest.raises(ControllerError):
        load_policy(repo.repo)


def test_a_blocked_venv_handoff_does_not_add_an_intent_on_every_pass(repo, monkeypatch):
    from dataclasses import replace
    from datetime import UTC, datetime, timedelta

    from worktree_controller.git_backend import inventory

    path, allocation_id, _head = _frozen_handoff(repo, "venv-storm")
    _create_reproducible_venv(path)

    def refuse(*_args, **_kwargs):
        raise ControllerError("PROJECT_IDENTITY_MISMATCH", "fixture: deterministic refusal")

    monkeypatch.setattr(lifecycle, "register_disposable", refuse)
    store = MetadataStore(repo.repo / ".git")

    def attempt():
        snapshot = store.load()
        item = next(i for i in inventory(repo.repo).worktrees if i.path == path)
        with pytest.raises(ControllerError) as refused:
            convergence._register_venv(repo.repo, item, snapshot.records[allocation_id])
        return refused.value.code

    def intents():
        return [i for i in store.load().release_intents.values() if i.allocation_id == allocation_id]

    def stale_all(age=0):
        stamp = (datetime.now(UTC) - timedelta(seconds=age)).isoformat(timespec="seconds")
        for intent in intents():
            if intent.status != "STALE":
                store.replace_release_intent(replace(
                    intent, status="STALE", blocker="PROJECT_IDENTITY_MISMATCH", updated_at=stamp))

    assert attempt() == "PROJECT_IDENTITY_MISMATCH" and len(intents()) == 1
    assert attempt() == "PROJECT_IDENTITY_MISMATCH" and len(intents()) == 1  # live intent reused
    stale_all()
    assert attempt() == "PROJECT_IDENTITY_MISMATCH" and len(intents()) == 2  # first expiry renews
    stale_all()
    for _ in range(3):
        assert attempt() == "RENEWAL_BACKOFF"
    assert len(intents()) == 2
    aged = (datetime.now(UTC) - timedelta(hours=2)).isoformat(timespec="seconds")
    for intent in intents():
        store.replace_release_intent(replace(intent, updated_at=aged))
    assert attempt() == "PROJECT_IDENTITY_MISMATCH" and len(intents()) == 3  # retried after backoff
