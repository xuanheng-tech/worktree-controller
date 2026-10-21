import hashlib
import json
import uuid
from pathlib import Path

import pytest

from tests.test_cleanup_recovery import interrupted as interrupted
from tests.test_cleanup_recovery import prepare, preserve
from tests.test_controlled import acquire_one
from tests.test_shadow import git
from tests.test_source_cleanup import generation_bytes, ready
from tests.test_source_preservation import source as source
from tests.test_typed_disposable import target as target
from worktree_controller import cleanup_evidence as e
from worktree_controller import cleanup_recovery as legacy
from worktree_controller import lifecycle
from worktree_controller import source_cleanup as c
from worktree_controller import source_preservation as s
from worktree_controller import source_recovery as r
from worktree_controller import transaction_scope as scope
from worktree_controller.controller import doctor
from worktree_controller.models import ControllerError
from worktree_controller.store import now_iso


def test_retained_terminal_legacy_journal_does_not_block_new_source_cleanup(interrupted):
    repository, _, _, store, context, _, old_bytes, _ = interrupted
    ref = prepare(interrupted, preserve(interrupted), execute=True)["decision"]["recovery_receipt"]
    result = legacy.resume(
        repository.repo, recovery_path=Path(ref["path"]), recovery_sha=ref["sha256"], dry_run=False
    )
    assert result["decision"]["code"] == "RECOVERY_CLEANED"
    assert legacy.completed(store, context["original_transaction_id"])
    assert doctor(repository.repo)["decision"]["transactions"]["historical_terminal"] == [
        {"transaction_id": context["original_transaction_id"], "state": "RECOVERY_CLEANED"}
    ]
    worktree, acquired = acquire_one(
        repository, task="tracked-source-after-terminal", authority="tracked-source-after-terminal"
    )
    (worktree / "frontend/index.html").write_text("unique dirty source, synthetic fixture\n")
    plan = s.plan_preservation(
        repository.repo,
        worktree,
        acquired["decision"]["allocation_id"],
        "module-owner",
        1,
        expected_head=git(worktree, "rev-parse", "HEAD"),
    )
    preserved = s.copy_preservation(plan)
    cleanup = c.plan_cleanup(plan, preserved["receipt_sha256"])
    result = c.cleanup_source(cleanup)
    assert result["result"] == "SOURCE_CLEANUP_COMPLETE"
    assert all(path.read_bytes() == data for path, data in old_bytes.items())


def fail_before_action(source, monkeypatch, cleanup=None):
    cleanup = cleanup or ready(source)[0]

    def refusal(*args, **kwargs):
        raise scope.TransactionConflict(
            [
                {
                    "transaction_id": str(uuid.UUID(int=8)),
                    "lifecycle": "NONTERMINAL",
                    "reason": "synthetic coordinator scope refusal",
                }
            ]
        )

    with monkeypatch.context() as patch:
        patch.setattr(c, "_state", refusal)
        failed = c.cleanup_source(cleanup)
    assert failed["result"] == "SOURCE_CLEANUP_BLOCKED"
    assert failed["code"] == "INTERRUPTED_TRANSACTION"
    receipt, _ = e.read(Path(failed["receipt"]["path"]), failed["receipt"]["sha256"])
    assert receipt["actions"] == []
    return cleanup, failed


def record(source, failed):
    args = (source[0].repo, failed["transaction_id"], failed["receipt"]["sha256"])
    plan = r.plan_reconciliation(*args)
    return plan, r.reconcile(*args, plan["plan_id"])


@pytest.mark.parametrize("count", [1, 3])
def test_same_allocation_terminal_journals_coexist_and_successor_does_not_replay(
    source, monkeypatch, count
):
    cleanup = ready(source)[0]
    plan = cleanup["preservation_plan"]
    store = c._store(plan)
    original_generation = generation_bytes(Path(plan["destination"]))
    old = {}
    identities = []
    for _ in range(count):
        cleanup, failed = fail_before_action(source, monkeypatch, cleanup)
        identities.append(failed["transaction_id"])
        directory = store.root / "source-cleanup" / failed["transaction_id"]
        paths = [
            Path(failed["receipt"]["path"]),
            store.transactions_dir / f"{failed['transaction_id']}.json",
            directory / "plan.json",
        ]
        old.update({path: path.read_bytes() for path in paths})
        assert any(
            w["code"] == "INTERRUPTED_TRANSACTION" for w in doctor(source[0].repo)["warnings"]
        )
        planned, recovery = record(source, failed)
        assert planned["state"] == r.STATE
        assert recovery["new_attempt_id"] not in identities
        value, _ = e.read(
            Path(recovery["reconciliation"]["path"]), recovery["reconciliation"]["sha256"]
        )
        assert value["actions"] == [] and value["safe_to_supersede"]
        assert value["original_attempt_will_replay"] is False
        old[Path(recovery["reconciliation"]["path"])] = Path(
            recovery["reconciliation"]["path"]
        ).read_bytes()
        with store.lock(exclusive=False):
            assert scope.terminal(store, failed["transaction_id"], verify_generations=True)
            assert scope.pending(store) == []
        diagnostic = doctor(source[0].repo)["decision"]["transactions"]
        assert diagnostic["unresolved_count"] == 0
        assert {row["state"] for row in diagnostic["historical_terminal"]} == {
            "SOURCE_ATTEMPT_SUPERSEDED"
        }
        assert not any(
            w["code"] == "INTERRUPTED_TRANSACTION" for w in doctor(source[0].repo)["warnings"]
        )
        cleanup = c.plan_cleanup(plan, cleanup["preservation_receipt_sha256"])
        assert cleanup["reconciliation"]["new_attempt_id"] == recovery["new_attempt_id"]
    done = c.cleanup_source(cleanup)
    assert done["result"] == "SOURCE_CLEANUP_COMPLETE"
    assert done["transaction_id"] == recovery["new_attempt_id"]
    assert done["transaction_id"] not in identities
    assert all(path.read_bytes() == content for path, content in old.items())
    assert generation_bytes(Path(plan["destination"])) == original_generation
    assert len(list(store.transactions_dir.iterdir())) == count
    with store.lock(exclusive=False):
        assert not scope.pending(store)
    with pytest.raises(ControllerError):
        c.cleanup_source(cleanup)


@pytest.mark.parametrize("drift", ["source", "index", "preservation"])
def test_new_attempt_rechecks_baseline_after_reconciliation(source, monkeypatch, drift):
    cleanup, failed = fail_before_action(source, monkeypatch)
    _, recovered = record(source, failed)
    retry = c.plan_cleanup(cleanup["preservation_plan"], cleanup["preservation_receipt_sha256"])
    preserved = Path(cleanup["preservation_plan"]["destination"]) / "tree/src/plain.py"
    if drift == "source":
        (source[1] / "src/plain.py").write_bytes(b"new dirty content")
    elif drift == "index":
        git(source[1], "add", "src/plain.py")
    else:
        preserved.chmod(0o600)
        preserved.write_bytes(b"changed preservation")
    with pytest.raises(ControllerError):
        c.cleanup_source(retry)
    store = c._store(cleanup["preservation_plan"])
    assert not (store.root / "source-cleanup" / recovered["new_attempt_id"]).exists()


@pytest.mark.parametrize("other", [False, True])
def test_nonterminal_conflicts_follow_actual_allocation_and_path_scope(source, other):
    cleanup = ready(source)[0]
    identity = cleanup["preservation_plan"]["authority"]["identity"]
    store = c._store(cleanup["preservation_plan"])
    transaction = str(uuid.uuid4())
    allocation = str(uuid.uuid4()) if other else identity["allocation_id"]
    journal = lifecycle._transaction(
        command="acquire",
        transaction_id=transaction,
        repository_id=identity["repository_id"],
        target_path=source[0].root / "other" if other else source[1],
        allocation_id=allocation,
        branch="fixture",
        expected_head=identity["expected_head"],
        expected_record_version=1,
        phase="PREPARED",
        created_at=now_iso(),
    )
    ref = e.write(store.transactions_dir, transaction + ".json", journal)
    if other:
        assert c.cleanup_source(cleanup)["result"] == "SOURCE_CLEANUP_COMPLETE"
    else:
        with pytest.raises(scope.TransactionConflict) as blocked:
            c.cleanup_source(cleanup)
        assert blocked.value.transaction_conflicts[0]["allocation_id"] == allocation
        assert blocked.value.transaction_conflicts[0]["transaction_id"] == transaction
        assert s._scan(identity) == cleanup["preservation_plan"]["source_tree"]
    e.read(Path(ref["path"]), ref["sha256"])


@pytest.mark.parametrize("bad", ["malformed", "unknown-phase", "wrong-repository", "unknown-entry"])
def test_unknown_transaction_identity_or_lifecycle_fails_closed(source, bad):
    cleanup = ready(source)[0]
    identity = cleanup["preservation_plan"]["authority"]["identity"]
    store = c._store(cleanup["preservation_plan"])
    store.transactions_dir.mkdir(exist_ok=True)
    tx = str(uuid.uuid4())
    value = lifecycle._transaction(
        command="acquire",
        transaction_id=tx,
        repository_id=identity["repository_id"],
        target_path=source[1],
        allocation_id=identity["allocation_id"],
        branch="fixture",
        expected_head=identity["expected_head"],
        expected_record_version=1,
        phase="PREPARED",
        created_at=now_iso(),
    )
    if bad == "unknown-phase":
        value["phase"] = "UNKNOWN"
    if bad == "wrong-repository":
        value["repository_id"] = str(uuid.uuid4())
    path = store.transactions_dir / (tx + (".json" if bad != "unknown-entry" else ".other"))
    path.write_text("{" if bad == "malformed" else json.dumps(value))
    with pytest.raises(ControllerError):
        c.cleanup_source(cleanup)
    assert doctor(source[0].repo)["decision"]["code"] == "FAIL"


@pytest.mark.parametrize(
    "drift",
    [
        "source",
        "inode",
        "mode",
        "index",
        "head",
        "new-path",
        "generation",
        "receipt",
        "journal",
        "plan",
    ],
)
def test_no_action_reconciliation_requires_exact_current_baseline(source, monkeypatch, drift):
    cleanup, failed = fail_before_action(source, monkeypatch)
    store = c._store(cleanup["preservation_plan"])
    path = source[1] / "src/plain.py"
    if drift == "source":
        path.write_bytes(b"changed source")
    elif drift == "inode":
        saved = source[0].root / "saved-source"
        path.rename(saved)
        path.write_bytes(saved.read_bytes())
    elif drift == "mode":
        path.chmod(0o700)
    elif drift == "index":
        git(source[1], "add", "src/plain.py")
    elif drift == "head":
        git(source[1], "commit", "--allow-empty", "-m", "different HEAD")
    elif drift == "new-path":
        (source[1] / "new-source").write_text("new")
    else:
        if drift == "generation":
            path = Path(cleanup["preservation_plan"]["destination"]) / "tree/src/plain.py"
        elif drift == "receipt":
            path = Path(failed["receipt"]["path"])
        elif drift == "journal":
            path = store.transactions_dir / (failed["transaction_id"] + ".json")
        else:
            path = store.root / "source-cleanup" / failed["transaction_id"] / "plan.json"
        path.chmod(0o600)
        path.write_bytes(path.read_bytes() + b"\n")
        path.chmod(0o400)
    with pytest.raises(ControllerError):
        r.plan_reconciliation(source[0].repo, failed["transaction_id"], failed["receipt"]["sha256"])
    assert not (
        store.root / "source-cleanup" / failed["transaction_id"] / "reconciliation.json"
    ).exists()


@pytest.mark.parametrize("action", ["nonempty-actions", "before-evidence", "unknown-evidence"])
def test_no_action_cannot_be_inferred_from_empty_receipt_alone(source, monkeypatch, action):
    cleanup, failed = fail_before_action(source, monkeypatch)
    store = c._store(cleanup["preservation_plan"])
    receipt = Path(failed["receipt"]["path"])
    if action == "nonempty-actions":
        payload = json.loads(receipt.read_bytes())
        payload["actions"] = [{"path": "src/plain.py", "status": "ACTION_STARTED"}]
        receipt.chmod(0o600)
        receipt.write_text(json.dumps(payload))
        receipt.chmod(0o400)
    else:
        name = "0000-before.json" if action == "before-evidence" else "unrecognized.json"
        e.write(
            store.root / "source-cleanup" / failed["transaction_id"],
            name,
            {"execution_state": "ACTION_STARTED"},
        )
    with pytest.raises(ControllerError):
        r.plan_reconciliation(
            source[0].repo,
            failed["transaction_id"],
            hashlib.sha256(receipt.read_bytes()).hexdigest(),
        )


@pytest.mark.parametrize(
    "field", ["managed_references", "observed_references", "managed_visibility_uncertainties"]
)
def test_reconciliation_runtime_authority_stays_fail_closed(source, monkeypatch, field):
    from tests.test_typed_disposable import empty_runtime_evidence

    _, failed = fail_before_action(source, monkeypatch)
    runtime = empty_runtime_evidence()
    runtime[field] = [{"pid": 99999999, "kind": "fixture"}]
    monkeypatch.setattr(lifecycle, "runtime_authority_evidence", lambda *a, **k: runtime)
    with pytest.raises(s.AuthorityBlocked):
        r.plan_reconciliation(source[0].repo, failed["transaction_id"], failed["receipt"]["sha256"])


def test_append_only_collision_and_changed_reconciliation_block(source, monkeypatch):
    cleanup, failed = fail_before_action(source, monkeypatch)
    planned, recorded = record(source, failed)
    path = Path(recorded["reconciliation"]["path"])
    old = path.read_bytes()
    with pytest.raises(ControllerError):
        r.reconcile(
            source[0].repo,
            failed["transaction_id"],
            failed["receipt"]["sha256"],
            planned["plan_id"],
        )
    assert path.read_bytes() == old
    retry = c.plan_cleanup(cleanup["preservation_plan"], cleanup["preservation_receipt_sha256"])
    payload = json.loads(old)
    payload["safe_to_supersede"] = False
    path.chmod(0o600)
    path.write_text(json.dumps(payload))
    path.chmod(0o400)
    with pytest.raises(ControllerError):
        c.cleanup_source(retry)


def test_rollout_can_verify_no_action_without_mutation(source, monkeypatch):
    cleanup, failed = fail_before_action(source, monkeypatch)
    store = c._store(cleanup["preservation_plan"])
    before = generation_bytes(store.root / "source-cleanup")
    result = legacy.rollout_check(source[0].repo)
    assert result["decision"]["code"] == "ROLLOUT_RECOVERY_COMPATIBLE"
    assert result["decision"]["transactions"][0]["state"] == r.STATE
    assert generation_bytes(store.root / "source-cleanup") == before
    with store.lock(exclusive=False):
        assert scope.pending(store)
    record(source, failed)
    result = legacy.rollout_check(source[0].repo)
    assert result["decision"]["transactions"][0]["state"] == "SOURCE_ATTEMPT_SUPERSEDED"


def test_malformed_terminal_evidence_is_diagnostic_failure(source, monkeypatch):
    cleanup, failed = fail_before_action(source, monkeypatch)
    _, recovered = record(source, failed)
    path = Path(recovered["reconciliation"]["path"])
    value = json.loads(path.read_bytes())
    del value["original_evidence"]
    path.chmod(0o600)
    path.write_text(json.dumps(value))
    with pytest.raises(ControllerError):
        c.plan_cleanup(cleanup["preservation_plan"], cleanup["preservation_receipt_sha256"])
    diagnostic = doctor(source[0].repo)
    assert diagnostic["decision"]["code"] == "FAIL"
    assert any(item["code"] == "INVALID_TRANSACTION" for item in diagnostic["errors"])


def test_evidence_write_failure_preserves_original_conflict(source, monkeypatch):
    cleanup = ready(source)[0]
    original = e.write

    def write(directory, name, value):
        if directory.name == "completion-receipts":
            raise OSError(28, "synthetic evidence disk failure")
        return original(directory, name, value)

    monkeypatch.setattr(
        c,
        "_state",
        lambda *a, **k: (_ for _ in ()).throw(
            scope.TransactionConflict([{"transaction_id": "fixture-conflict"}])
        ),
    )
    monkeypatch.setattr(e, "write", write)
    failed = c.cleanup_source(cleanup)
    assert failed["receipt"] is None
    assert failed["code"] == "INTERRUPTED_TRANSACTION"
    assert failed["underlying"]["transaction_conflicts"] == [{"transaction_id": "fixture-conflict"}]
    assert failed["evidence_write_error"]["exception"]["errno"] == 28


def test_reconciled_attempt_then_typed_cleanup_and_release_keeps_all_evidence(source, monkeypatch):
    monkeypatch.setattr(lifecycle, "runtime_references", lambda *a, **k: ())
    monkeypatch.setattr(lifecycle, "editable_references", lambda *a, **k: ())
    (source[1] / "frontend/dist/assets").mkdir(parents=True)
    (source[1] / "frontend/dist/index.html").write_text("<html></html>")
    (source[1] / "frontend/dist/assets/index.js").write_text("/* fixture build */")
    cleanup, failed = fail_before_action(source, monkeypatch)
    store = c._store(cleanup["preservation_plan"])
    _, recovered = record(source, failed)
    old = generation_bytes(store.root / "source-cleanup")
    retry = c.plan_cleanup(cleanup["preservation_plan"], cleanup["preservation_receipt_sha256"])
    assert c.cleanup_source(retry)["result"] == "SOURCE_CLEANUP_COMPLETE"
    done = lifecycle.close_preserved(
        source[0].repo, target=source[1], expected_record_version=1, owner="module-owner"
    )
    assert done["decision"]["code"] == "PRESERVED"
    options = dict(target=source[1], expected_record_version=2, owner="module-owner")
    dry = lifecycle.cleanup_typed_disposable(source[0].repo, **options, dry_run=True)
    assert dry["decision"]["plan"]["blockers"] == []
    assert (
        lifecycle.cleanup_typed_disposable(
            source[0].repo,
            **options,
            dry_run=False,
            expected_plan_id=dry["decision"]["plan"]["plan_id"],
        )["decision"]["code"]
        == "TYPED_DISPOSABLE_CLEANED"
    )
    assert (
        lifecycle.release(
            source[0].repo,
            target=source[1],
            expected_record_version=2,
            owner_release="module-owner",
        )["decision"]["code"]
        == "RELEASED"
    )
    assert not source[1].exists()
    assert all(
        generation_bytes(store.root / "source-cleanup")[key] == value for key, value in old.items()
    )
    with store.lock(exclusive=False):
        assert scope.terminal(store, failed["transaction_id"], verify_generations=True)
    assert not any(
        w["code"] == "INTERRUPTED_TRANSACTION" for w in doctor(source[0].repo)["warnings"]
    )


def fail_after_restore(source, monkeypatch, completed=1):
    cleanup = ready(source)[0]
    original = c._state
    calls = 0

    def state(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2 * completed + 2:
            raise s.AuthorityBlocked("RUNTIME_SCAN_UNCERTAIN", {"synthetic": True})
        return original(*args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(c, "_state", state)
        failed = c.cleanup_source(cleanup, state_root=source[2])
    assert failed["code"] == "RUNTIME_SCAN_UNCERTAIN"
    receipt, _ = e.read(Path(failed["receipt"]["path"]), failed["receipt"]["sha256"])
    assert [a["status"] for a in receipt["actions"]] == (
        ["ACTION_COMPLETED"] * completed + ["ACTION_STARTED"]
    )
    return cleanup, failed


def progress_args(source, failed):
    return source[0].repo, failed["transaction_id"], failed["receipt"]["sha256"]


def original_evidence(cleanup, failed):
    store = c._store(cleanup["preservation_plan"])
    paths = [
        Path(failed["receipt"]["path"]),
        store.transactions_dir / f"{failed['transaction_id']}.json",
    ]
    paths.extend((store.root / "source-cleanup" / failed["transaction_id"]).iterdir())
    return {path: path.read_bytes() for path in paths}


@pytest.mark.parametrize("completed", [0, 1])
def test_progress_closes_only_attempt_and_preserves_current_source_and_old_evidence(
    source, monkeypatch, completed
):
    cleanup, failed = fail_after_restore(source, monkeypatch, completed)
    args = progress_args(source, failed)
    store = c._store(cleanup["preservation_plan"])
    old = original_evidence(cleanup, failed)
    generation = generation_bytes(Path(cleanup["preservation_plan"]["destination"]))
    source_before = {p: p.read_bytes() for p in (source[1] / "src").iterdir()}
    identities = {p: s.p._identity(p.stat()) for p in source_before}
    with pytest.raises(ControllerError):
        r.plan_reconciliation(*args)
    with pytest.raises(ControllerError):
        legacy.rollout_check(source[0].repo)  # No unresolved progress-plan installation bypass.
    plan = r.plan_progress(*args)
    assert (
        plan["remaining_preserved_paths"]
        == [a["path"] for a in cleanup["restore"]][completed + 1 :]
    )
    result = r.reconcile_progress(*args, plan["plan_id"])
    assert result["result"] == r.PROGRESS_STATE
    assert result["release_authorized"] is result["successor_authorized"] is False
    assert all(p.read_bytes() == data for p, data in old.items())
    assert source_before == {p: p.read_bytes() for p in source_before}
    assert identities == {p: s.p._identity(p.stat()) for p in source_before}
    assert generation_bytes(Path(cleanup["preservation_plan"]["destination"])) == generation
    assert (
        r.authorization(store, cleanup["preservation_plan"], cleanup["preservation_receipt_sha256"])
        is None
    )
    with store.lock(exclusive=False):
        assert (
            scope.terminal_state(store, failed["transaction_id"], verify_generations=True)
            == r.PROGRESS_STATE
        )
        assert scope.pending(store) == []
    assert (
        legacy.rollout_check(source[0].repo)["decision"]["transactions"][0]["state"]
        == r.PROGRESS_STATE
    )
    item, rec = lifecycle._managed_record(
        lifecycle.inventory(source[0].repo), store.load(), source[1]
    )
    assert c.verify_completed(store, item, rec) == []
    with pytest.raises(ControllerError):
        lifecycle.close_preserved(
            source[0].repo, target=source[1], owner="module-owner", expected_record_version=1
        )
    with pytest.raises(ControllerError):
        lifecycle.complete_no_change(
            source[0].repo, target=source[1], owner="module-owner", expected_record_version=1
        )
    with pytest.raises(ControllerError):
        lifecycle.release(
            source[0].repo,
            target=source[1],
            owner_release="module-owner",
            expected_record_version=1,
        )
    with pytest.raises(ControllerError):
        r.reconcile_progress(*args, plan["plan_id"])
    assert (
        store.root / "source-cleanup" / failed["transaction_id"] / f"{completed:04d}-after.json"
    ).exists() is False
    # History is immutable, while the active task may legitimately continue editing.
    (source[1] / "src/plain.py").write_bytes(b"legitimate later task edit\n")
    with store.lock(exclusive=False):
        assert (
            scope.terminal_state(store, failed["transaction_id"], verify_generations=True)
            == r.PROGRESS_STATE
        )
    assert not any(
        w["code"] == "INTERRUPTED_TRANSACTION" for w in doctor(source[0].repo)["warnings"]
    )


@pytest.mark.parametrize(
    "drift",
    [
        "started-content",
        "started-inode",
        "started-hardlink",
        "remaining",
        "index",
        "head",
        "generation",
        "unknown-after",
        "old-after",
        "plan-id",
    ],
)
def test_progress_rejects_drift_without_source_or_marker_writes(source, monkeypatch, drift):
    import os

    cleanup, failed = fail_after_restore(source, monkeypatch)
    args = progress_args(source, failed)
    plan = r.plan_progress(*args)
    store = c._store(cleanup["preservation_plan"])
    directory = store.root / "source-cleanup" / failed["transaction_id"]
    started = source[1] / cleanup["restore"][1]["path"]
    if drift == "started-content":
        started.write_bytes(b"partial")
    elif drift == "started-inode":
        saved = source[0].root / "old-started"
        started.rename(saved)
        started.write_bytes(saved.read_bytes())
    elif drift == "started-hardlink":
        os.link(started, source[0].root / "external-link")
    elif drift == "remaining":
        (source[1] / cleanup["restore"][2]["path"]).write_bytes(b"new content")
    elif drift == "index":
        git(source[1], "add", ".")
    elif drift == "head":
        git(source[1], "commit", "--allow-empty", "-m", "new HEAD")
    elif drift == "generation":
        pth = Path(cleanup["preservation_plan"]["destination"]) / "tree/src/plain.py"
        pth.chmod(0o600)
        pth.write_bytes(b"different preservation")
    elif drift == "unknown-after":
        e.write(directory, "0001-after.json", {"execution_state": "ACTION_COMPLETED"})
    elif drift == "old-after":
        pth = directory / "0000-after.json"
        pth.chmod(0o600)
        pth.write_bytes(pth.read_bytes() + b"\n")
    else:
        plan["plan_id"] = "0" * 64
    before = {p: p.read_bytes() for p in (source[1] / "src").iterdir()}
    with pytest.raises(ControllerError):
        r.reconcile_progress(*args, plan["plan_id"])
    assert before == {p: p.read_bytes() for p in before}
    assert not (directory / "progress-reconciliation.json").exists()


@pytest.mark.parametrize(
    "bad",
    [
        "checks-order",
        "checks-replace",
        "prefix",
        "cross-reference",
        "missing-after",
        "started-after",
    ],
)
def test_progress_requires_exact_action_prefix_and_check_chain(source, monkeypatch, bad):
    cleanup, failed = fail_after_restore(source, monkeypatch)
    receipt = Path(failed["receipt"]["path"])
    value = json.loads(receipt.read_bytes())
    if bad == "checks-order":
        value["checks"].reverse()
    elif bad == "checks-replace":
        value["checks"][1] = value["checks"][0]
    elif bad == "prefix":
        value["actions"].reverse()
    elif bad == "cross-reference":
        value["actions"][1]["before"] = value["actions"][0]["before"]
    elif bad == "missing-after":
        del value["actions"][0]["after"]
    else:
        value["actions"][1]["after"] = value["actions"][0]["after"]
    receipt.chmod(0o600)
    receipt.write_text(json.dumps(value))
    sha = hashlib.sha256(receipt.read_bytes()).hexdigest()
    with pytest.raises(ControllerError):
        r.plan_progress(source[0].repo, failed["transaction_id"], sha)


@pytest.mark.parametrize(
    "field",
    [
        "release_authorized",
        "successor_authorized",
        "original_attempt_will_replay",
        "historical_actions",
        "observed_restored_paths",
        "remaining_preserved_paths",
        "original_evidence",
    ],
)
def test_progress_terminal_rejects_tampering_even_with_recomputed_plan_hash(
    source, monkeypatch, field
):
    cleanup, failed = fail_after_restore(source, monkeypatch)
    args = progress_args(source, failed)
    planned = r.plan_progress(*args)
    result = r.reconcile_progress(*args, planned["plan_id"])
    path = Path(result["reconciliation"]["path"])
    value = json.loads(path.read_bytes())
    if isinstance(value[field], bool):
        value[field] = not value[field]
    elif isinstance(value[field], list):
        value[field] = []
    else:
        value[field] = {}
    value["plan_id"] = s.p._sha(r._core(value))
    path.chmod(0o600)
    path.write_text(json.dumps(value))
    with pytest.raises((ControllerError, KeyError)):
        r.terminal(c._store(cleanup["preservation_plan"]), failed["transaction_id"])
    assert doctor(source[0].repo)["decision"]["code"] == "FAIL"


def test_progress_locked_apply_rechecks_runtime_and_preserves_failure_evidence(source, monkeypatch):
    from tests.test_typed_disposable import empty_runtime_evidence

    cleanup, failed = fail_after_restore(source, monkeypatch)
    args = progress_args(source, failed)
    planned = r.plan_progress(*args)
    old = original_evidence(cleanup, failed)
    original = r.plan_progress

    def changed(*a, **kwargs):
        if kwargs.get("_migration_store"):
            runtime = empty_runtime_evidence()
            runtime["managed_references"] = [{"pid": 99999999, "kind": "fixture"}]
            monkeypatch.setattr(lifecycle, "runtime_authority_evidence", lambda *a, **k: runtime)
        return original(*a, **kwargs)

    monkeypatch.setattr(r, "plan_progress", changed)
    with pytest.raises(s.AuthorityBlocked):
        r.reconcile_progress(*args, planned["plan_id"])
    assert all(p.read_bytes() == b for p, b in old.items())


@pytest.mark.parametrize("started", [True, False])
def test_full_restoration_cannot_be_disguised_as_partial_attempt_preservation(
    source, monkeypatch, started
):
    if started:
        cleanup, failed = fail_after_restore(source, monkeypatch, completed=2)
    else:
        cleanup = ready(source)[0]
        original = c._state
        calls = 0

        def state(*a, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 7:
                raise s.AuthorityBlocked("RUNTIME_SCAN_UNCERTAIN", {"synthetic": True})
            return original(*a, **kwargs)

        with monkeypatch.context() as patch:
            patch.setattr(c, "_state", state)
            failed = c.cleanup_source(cleanup, state_root=source[2])
        assert failed["code"] == "RUNTIME_SCAN_UNCERTAIN"
    assert git(source[1], "status", "--porcelain") == ""
    old = original_evidence(cleanup, failed)
    with pytest.raises(ControllerError):
        r.plan_progress(*progress_args(source, failed))
    assert all(p.read_bytes() == data for p, data in old.items())


def test_progress_evidence_write_failure_has_no_terminal_or_source_side_effect(source, monkeypatch):
    cleanup, failed = fail_after_restore(source, monkeypatch)
    args = progress_args(source, failed)
    planned = r.plan_progress(*args)
    old = original_evidence(cleanup, failed)
    source_before = {p: p.read_bytes() for p in (source[1] / "src").iterdir()}
    original = e.write

    def write(directory, name, value):
        if name == "progress-reconciliation.json":
            raise OSError(28, "synthetic evidence disk failure")
        return original(directory, name, value)

    monkeypatch.setattr(e, "write", write)
    with pytest.raises(OSError):
        r.reconcile_progress(*args, planned["plan_id"])
    assert all(p.read_bytes() == b for p, b in old.items())
    assert source_before == {p: p.read_bytes() for p in source_before}
    assert not r.terminal(c._store(cleanup["preservation_plan"]), failed["transaction_id"])
