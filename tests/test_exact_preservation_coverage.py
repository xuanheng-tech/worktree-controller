"""Exact generations cover every cleanup resource without widening release authority."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import uuid
from dataclasses import replace
from pathlib import Path

import pytest

from tests.test_cleanup_attempts import pending_typed as pending_typed
from tests.test_controlled import acquire_one, adopt_canonical
from tests.test_retention_admission import POLICY_V5
from tests.test_shadow import GitRepository, git
from tests.test_typed_disposable import RESOURCE_TYPES, empty_runtime_evidence, fingerprint
from tests.test_typed_disposable import target as target
from worktree_controller import lifecycle
from worktree_controller import preservation as p
from worktree_controller import preserved_cleanup as pc
from worktree_controller.delivered_closeout import archive_retained, reconcile_no_change_completion
from worktree_controller.git_backend import inventory
from worktree_controller.models import ControllerError
from worktree_controller.store import MetadataStore
from worktree_controller.writer_lease import describe_holder, new_lease


@pytest.fixture(params=["feature", "audit"])
def no_change_cleaned(request, tmp_path, monkeypatch):
    monkeypatch.setattr(lifecycle, "runtime_references", lambda *a, **kw: ())
    monkeypatch.setattr(lifecycle, "editable_references", lambda *a, **kw: ())
    monkeypatch.setattr(lifecycle, "runtime_authority_evidence", lambda *a, **kw: empty_runtime_evidence())
    monkeypatch.setattr(p, "evidence_root", lambda: tmp_path / "preservations")
    with GitRepository(linked=0, policy_text=POLICY_V5) as repository:
        (repository.repo / ".gitignore").write_text("unknown/\n")
        git(repository.repo, "add", ".gitignore")
        git(repository.repo, "commit", "-m", "synthetic ignored build output")
        adopt_canonical(repository)
        target, acquired = acquire_one(repository, role=request.param)
        allocation = acquired["decision"]["allocation_id"]
        lifecycle.park(repository.repo, target=target, expected_record_version=1, owner="module-owner")
        (target / "unknown").mkdir()
        (target / "unknown/generated.js").write_text("synthetic build output\n")
        copied = _preserve(repository, target, allocation, "unknown")
        planned = _cleanup(repository, target, (copied,))
        assert not planned["plan"]["blockers"]
        cleaned = _cleanup(repository, target, (copied,), plan_id=planned["plan"]["plan_id"])
        store = MetadataStore(inventory(repository.repo).common_dir)
        path, receipt = _receipt(store, cleaned["receipt_id"])
        yield repository, target, allocation, store, path, receipt, copied


def _no_change_close(state, *, legacy=False):
    repository, target, allocation, store, *_ = state
    result = lifecycle.complete_no_change(repository.repo, target=target,
        expected_record_version=2, owner="module-owner")["decision"]
    path, receipt = _receipt(store, result["completion_receipt_id"])
    if legacy:
        for key in ("contract", "before_record", "target_path", "preservation_cleanup_receipts", "completion_event"):
            receipt.pop(key)
        path.chmod(0o600)
        path.write_text(json.dumps(receipt))
        record = store.load().records[allocation]
        event = dict(record.history[-1])
        event.pop("no_change_completion_ref")
        store.replace_record(replace(record, history=(*record.history[:-1], event)))
    return path, receipt


def _no_change_recovery(state, completion, *, execute=False, plan_id=None, **changes):
    repository, target, _, _, cleanup_path, receipt, _ = state
    args = dict(target=target, owner="module-owner", expected_record_version=3,
        completion_id=completion[1]["receipt_id"],
        completion_sha256=hashlib.sha256(completion[0].read_bytes()).hexdigest(),
        cleanup_ids=(receipt["receipt_id"],),
        cleanup_sha256=(hashlib.sha256(cleanup_path.read_bytes()).hexdigest(),),
        dry_run=not execute, expected_plan_id=plan_id)
    args.update(changes)
    return reconcile_no_change_completion(repository.repo, **args)["decision"]


def test_no_change_freezes_exact_cleanup_then_releases_without_changing_proof(no_change_cleaned):
    repository, target, allocation, store, path, _, copied = no_change_cleaned
    original = path.read_bytes()
    completed = _no_change_close(no_change_cleaned)
    assert completed[1]["before_record"]["record_version"] == 2
    assert completed[1]["preservation_cleanup_receipts"] == [{
        "receipt_id": path.stem, "path": str(path), "sha256": hashlib.sha256(original).hexdigest()}]
    head = git(target, "rev-parse", "HEAD")
    branch = git(target, "branch", "--show-current")
    for dry_run in (True, False):
        released = lifecycle.release(repository.repo, target=target, expected_record_version=3,
            owner_release="module-owner", dry_run=dry_run)
        assert not released["errors"]
    assert not target.exists()
    assert git(repository.repo, "rev-parse", branch) == head
    assert path.read_bytes() == original
    assert store.load().records[allocation].lifecycle == "RELEASED"
    assert pc.coverage(*copied, verify_source=False)[0]["receipt_sha256"] == copied[1]


def test_legacy_no_change_reconciliation_is_exact_append_only_and_idempotent(no_change_cleaned):
    repository, target, allocation, store, cleanup_path, *_ = no_change_cleaned
    completion = _no_change_close(no_change_cleaned, legacy=True)
    original = {path: path.read_bytes() for path in (cleanup_path, completion[0])}
    with pytest.raises(ControllerError, match="exact CAS"):
        lifecycle.release(repository.repo, target=target, expected_record_version=3,
            owner_release="module-owner", dry_run=True)
    planned = _no_change_recovery(no_change_cleaned, completion)
    assert planned["code"] == "NO_CHANGE_COMPLETION_RECOVERY_READY"
    before = fingerprint(store.root)
    assert _no_change_recovery(no_change_cleaned, completion) == planned
    assert fingerprint(store.root) == before
    recovered = _no_change_recovery(no_change_cleaned, completion, execute=True, plan_id=planned["plan_id"])
    assert recovered["record_version"] == 4
    before = fingerprint(store.root)
    duplicate = _no_change_recovery(no_change_cleaned, completion, execute=True, plan_id=planned["plan_id"])
    assert duplicate["code"] == "NO_CHANGE_COMPLETION_ALREADY_RECONCILED"
    assert duplicate["receipt"] == recovered["receipt"]
    assert store.load().records[allocation].record_version == 4
    assert fingerprint(store.root) == before
    for path, raw in original.items():
        assert path.read_bytes() == raw
    lifecycle.release(repository.repo, target=target, expected_record_version=4, owner_release="module-owner")
    assert not target.exists()


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("drift", ["cas-plus-two", "owner", "authority_key", "task_key", "base_commit", "head", "branch", "completion-bytes", "cleanup-bytes", "cleanup-missing", "generation", "source-reappeared", "history", "event-time", "event-extra", "history-empty"])
def test_no_change_bridge_rejects_drift_without_release(no_change_cleaned, legacy, drift):
    repository, target, allocation, store, cleanup_path, _, copied = no_change_cleaned
    completion = _no_change_close(no_change_cleaned, legacy=legacy)
    if legacy:
        planned = _no_change_recovery(no_change_cleaned, completion)
        _no_change_recovery(no_change_cleaned, completion, execute=True, plan_id=planned["plan_id"])
    record = store.load().records[allocation]
    version = record.record_version
    if drift == "cas-plus-two":
        version += 1
        store.replace_record(replace(record, record_version=version))
    elif drift in {"owner", "authority_key", "task_key", "base_commit"}:
        store.replace_record(replace(record, **{drift: "changed"}))
    elif drift == "history":
        event = dict(record.history[-1], **{"from": "ACTIVE"})
        store.replace_record(replace(record, history=(*record.history[:-1], event)))
    elif drift == "event-time":
        changed = "2099-01-01T00:00:00+00:00"
        event = dict(record.history[-1], at=changed)
        store.replace_record(replace(record, last_reviewed_at=changed, history=(*record.history[:-1], event)))
    elif drift == "event-extra":
        event = dict(record.history[-1], task_completed=False)
        store.replace_record(replace(record, history=(*record.history[:-1], event)))
    elif drift == "history-empty":
        store.replace_record(replace(record, history=()))
    elif drift == "cleanup-missing":
        cleanup_path.unlink()
    elif drift == "head":
        git(target, "commit", "--allow-empty", "-m", "synthetic head drift")
    elif drift == "branch":
        git(target, "switch", "-c", "synthetic-branch-drift")
    elif drift in {"completion-bytes", "cleanup-bytes"}:
        path = completion[0] if drift == "completion-bytes" else cleanup_path
        path.chmod(0o600)
        path.write_bytes(path.read_bytes() + b" ")
    elif drift == "generation":
        node = copied[0].parent / "tree/generated.js"
        node.chmod(0o600)
        node.write_bytes(b"changed preserved build")
    else:
        (target / "unknown").mkdir()
        (target / "unknown/generated.js").write_text("reappeared")
    before = fingerprint(store.root)
    with pytest.raises(ControllerError):
        lifecycle.release(repository.repo, target=target, expected_record_version=version,
            owner_release="changed" if drift == "owner" else "module-owner", dry_run=True)
    assert target.exists()
    assert fingerprint(store.root) == before


@pytest.mark.parametrize("drift", ["completion-sha", "cleanup-sha", "missing", "duplicate", "second-completion", "binding", "lease", "runtime", "transaction"])
def test_legacy_reconciliation_rejects_wrong_proof_and_live_authority(no_change_cleaned, monkeypatch, drift):
    repository, target, allocation, store, _, receipt, _ = no_change_cleaned
    completion = _no_change_close(no_change_cleaned, legacy=True)
    changes = {}
    if drift == "completion-sha":
        changes["completion_sha256"] = "0" * 64
    elif drift == "cleanup-sha":
        changes["cleanup_sha256"] = ("0" * 64,)
    elif drift == "missing":
        changes.update(cleanup_ids=(), cleanup_sha256=())
    elif drift == "duplicate":
        changes.update(cleanup_ids=(receipt["receipt_id"],) * 2, cleanup_sha256=("0" * 64,) * 2)
    elif drift == "second-completion":
        identity = str(uuid.uuid4())
        store.write_completion_receipt(identity, dict(completion[1], receipt_id=identity))
    elif drift == "binding":
        record = store.load().records[allocation]
        store.replace_record(replace(record, external_bindings=("synthetic-binding",)))
    elif drift == "lease":
        store.write_writer_lease(new_lease(repository_id=store.load().repository_id,
            allocation_id=allocation, worktree_path=str(target), owner="module-owner",
            holder=describe_holder(os.getpid(), client_id="manual"), ttl_seconds=None))
    elif drift == "runtime":
        evidence = empty_runtime_evidence()
        evidence["observed_references"].append({"kind": "cwd", "pid": 12345})
        monkeypatch.setattr(lifecycle, "runtime_authority_evidence", lambda *a, **kw: evidence)
    else:
        identity = str(uuid.uuid4())
        store.write_transaction(identity, {"transaction_id": identity, "command": "release",
            "target_path": str(target), "allocation_id": allocation, "phase": "PREPARED"})
    before = fingerprint(store.root)
    with pytest.raises(ControllerError):
        _no_change_recovery(no_change_cleaned, completion, **changes)
    assert target.exists()
    assert fingerprint(store.root) == before


@pytest.mark.parametrize("malformed", ["completion-id", "cleanup-id", "timestamp", "shape"])
def test_no_change_reconciliation_malformed_input_is_json_error(no_change_cleaned, capsys, malformed):
    from worktree_controller.cli import main

    repository, target, _, store, cleanup_path, *_ = no_change_cleaned
    completion_path, completion = _no_change_close(no_change_cleaned, legacy=True)
    if malformed in {"timestamp", "shape"}:
        if malformed == "timestamp":
            completion["completed_at"] = "not-an-iso-time"
        else:
            completion.pop("completed_at")
        completion_path.write_text(json.dumps(completion))
    before = fingerprint(store.root)
    result = main(["reconcile", "no-change-completion", "--repo", str(repository.repo),
        "--path", str(target), "--owner", "module-owner", "--expected-record-version", "3",
        "--completion-receipt-id", "malformed" if malformed == "completion-id" else completion_path.stem,
        "--completion-receipt-sha256", hashlib.sha256(completion_path.read_bytes()).hexdigest(),
        "--cleanup-receipt-id", "malformed" if malformed == "cleanup-id" else cleanup_path.stem,
        "--cleanup-receipt-sha256", hashlib.sha256(cleanup_path.read_bytes()).hexdigest(), "--dry-run", "--json"])
    output = capsys.readouterr()
    assert result == 2 and not output.err
    assert json.loads(output.out)["errors"][0]["code"] == "NO_CHANGE_CLOSEOUT_INVALID"
    assert fingerprint(store.root) == before


def test_no_change_reconciliation_replay_rejects_another_plan(no_change_cleaned):
    _, target, _, store, *_ = no_change_cleaned
    completion = _no_change_close(no_change_cleaned, legacy=True)
    plan = _no_change_recovery(no_change_cleaned, completion)
    _no_change_recovery(no_change_cleaned, completion, execute=True, plan_id=plan["plan_id"])
    before = fingerprint(store.root)
    with pytest.raises(ControllerError, match="original plan"):
        _no_change_recovery(no_change_cleaned, completion, execute=True, plan_id="0" * 64)
    assert target.exists() and fingerprint(store.root) == before


def test_no_change_reconciliation_reuses_write_once_orphan_receipt(no_change_cleaned, monkeypatch):
    _, target, allocation, store, *_ = no_change_cleaned
    completion = _no_change_close(no_change_cleaned, legacy=True)
    plan = _no_change_recovery(no_change_cleaned, completion)
    original_replace = MetadataStore.replace_record

    def interrupted_write(self, record):
        if record.allocation_id == allocation and record.record_version == 4:
            raise OSError("synthetic metadata write interruption")
        return original_replace(self, record)

    with monkeypatch.context() as patch:
        patch.setattr(MetadataStore, "replace_record", interrupted_write)
        with pytest.raises(ControllerError, match="unavailable or malformed"):
            _no_change_recovery(no_change_cleaned, completion, execute=True, plan_id=plan["plan_id"])
    assert store.load().records[allocation].record_version == 3
    path = store.completion_receipts_dir / f"{uuid.uuid5(uuid.UUID(allocation), plan['plan_id'])}.json"
    raw = path.read_bytes()
    recovered = _no_change_recovery(no_change_cleaned, completion, execute=True, plan_id=plan["plan_id"])
    assert recovered["receipt"]["sha256"] == hashlib.sha256(raw).hexdigest()
    assert path.read_bytes() == raw and target.exists()
    assert store.load().records[allocation].record_version == 4


def test_no_change_reconciliation_clock_rollback_refuses_before_receipt_write(no_change_cleaned, monkeypatch):
    import worktree_controller.store as storage

    _, target, allocation, store, *_ = no_change_cleaned
    completion = _no_change_close(no_change_cleaned, legacy=True)
    plan = _no_change_recovery(no_change_cleaned, completion)
    before = fingerprint(store.root)
    monkeypatch.setattr(storage, "now_iso", lambda: "1970-01-01T00:00:00+00:00")
    with pytest.raises(ControllerError, match="predates"):
        _no_change_recovery(no_change_cleaned, completion, execute=True, plan_id=plan["plan_id"])
    assert target.exists() and fingerprint(store.root) == before
    assert store.load().records[allocation].record_version == 3


@pytest.fixture
def exact_generations(target, tmp_path, monkeypatch):
    repository, worktree, allocation = target
    monkeypatch.setattr(p, "evidence_root", lambda: tmp_path / "preservations")
    for relative, content in (
        (".venv/state.bin", b"first opaque fixture\x00\xff"),
        ("unknown/state.bin", b"second opaque fixture\x00\xfe"),
    ):
        node = worktree / relative
        node.parent.mkdir(parents=True)
        node.write_bytes(content)
    copied = tuple(
        _preserve(repository, worktree, allocation, relative) for relative in (".venv", "unknown")
    )
    return repository, worktree, allocation, copied


def _preserve(repository, worktree, allocation, relative):
    plan = p.plan_preservation(
        repository.repo, worktree, allocation, "module-owner", 2, source_relative=relative
    )
    result = p.copy_preservation(plan)
    assert p.verify_preservation(plan)["verification"] == "PASS"
    return Path(result["receipt"]), result["receipt_sha256"]


def _cleanup(repository, worktree, coverage, *, plan_id=None, **kwargs):
    return lifecycle.cleanup_typed_disposable(
        repository.repo,
        target=worktree,
        expected_record_version=2,
        owner="module-owner",
        dry_run=plan_id is None,
        expected_plan_id=plan_id,
        preservation_receipt=coverage[0][0],
        preservation_receipt_sha256=coverage[0][1],
        additional_preservation_receipts=tuple(path for path, _sha in coverage[1:]),
        additional_preservation_receipt_sha256=tuple(sha for _path, sha in coverage[1:]),
        **kwargs,
    )["decision"]


def _assert_blocked(decision):
    assert decision["code"] == "RESOURCE_CLEANUP_BLOCKED"
    assert decision["plan"]["blockers"]
    assert decision["plan"]["cleanup_paths"] == []
    assert all(row["cleanup_action"] == "none" for row in decision["plan"]["resources"])


def _archive(repository, worktree):
    args = dict(
        target=worktree,
        expected_record_version=2,
        owner_release="module-owner",
        actor="governance",
    )
    planned = archive_retained(repository.repo, **args, dry_run=True)
    assert planned["decision"]["code"] == "ARCHIVE_READY"
    return archive_retained(repository.repo, **args, plan_id=planned["decision"]["plan_id"])


def _receipt(store, receipt_id):
    path = store.completion_receipts_dir / f"{receipt_id}.json"
    return path, json.loads(path.read_text())


def _resume_cleaned_handoff(state):
    repository, worktree, allocation, copied = state
    planned = _cleanup(repository, worktree, copied)
    cleaned = _cleanup(repository, worktree, copied, plan_id=planned["plan"]["plan_id"])
    store = MetadataStore(inventory(repository.repo).common_dir)
    receipt, _ = _receipt(store, cleaned["receipt_id"])
    version = 2
    for _ in range(2):
        lifecycle.resume(repository.repo, target=worktree, expected_record_version=version,
            owner="module-owner")
        version += 1
        lifecycle.park(repository.repo, target=worktree, expected_record_version=version,
            owner="module-owner")
        version += 1
    lifecycle.resume(repository.repo, target=worktree, expected_record_version=version,
        owner="module-owner")
    version += 1
    head = git(worktree, "rev-parse", "HEAD")
    lifecycle.handoff(repository.repo, target=worktree, expected_record_version=version,
        kind="integration-ready", commit=head, finalizer_record_id="synthetic-same-head-finalizer")
    git(repository.repo, "merge", "--ff-only", head)
    return repository, worktree, allocation, store, receipt, version + 1


@pytest.mark.parametrize("target", ["retained-feature", "retained-audit"], indirect=True)
def test_completed_cleanup_survives_same_head_resume_and_formal_handoff(exact_generations):
    repository, worktree, allocation, store, receipt, version = _resume_cleaned_handoff(
        exact_generations
    )
    original = receipt.read_bytes()
    proof = pc.cleanup_receipts(
        store, next(w for w in inventory(repository.repo).worktrees if w.path == worktree),
        store.load().records[allocation], store.load().repository_id,
    )
    assert proof == [{"receipt_id": receipt.stem, "path": str(receipt),
        "sha256": hashlib.sha256(original).hexdigest()}]
    lifecycle.release(repository.repo, target=worktree, expected_record_version=version,
        owner_release="module-owner")
    assert not worktree.exists()
    assert receipt.read_bytes() == original


@pytest.mark.parametrize("target", ["retained-feature"], indirect=True)
@pytest.mark.parametrize("drift", ["cas", "history-chain", "history-kind", "history-owner",
    "owner", "authority", "finalizer", "head", "source-reappeared", "action-proof"])
def test_resumed_cleanup_keeps_exact_history_and_immutable_proof_gates(exact_generations, drift):
    repository, worktree, allocation, store, receipt, version = _resume_cleaned_handoff(
        exact_generations
    )
    record = store.load().records[allocation]
    if drift == "cas":
        store.replace_record(replace(record, record_version=version + 1))
        version += 1
    elif drift.startswith("history-"):
        event = dict(record.history[2])
        event.update({"history-chain": {"from": "HANDOFF_READY"},
            "history-kind": {"event": "REVIEWED"},
            "history-owner": {"owner": "different-owner"}}[drift])
        store.replace_record(replace(record, history=(*record.history[:2], event, *record.history[3:])))
    elif drift in {"owner", "authority", "finalizer"}:
        key = {"owner": "owner", "authority": "authority_key", "finalizer": "finalizer_commit"}[drift]
        store.replace_record(replace(record, **{key: "changed"}))
    elif drift == "head":
        git(worktree, "commit", "--allow-empty", "-m", "synthetic changed head")
    elif drift == "source-reappeared":
        (worktree / "unknown").mkdir()
        (worktree / "unknown/state.bin").write_bytes(b"new uncleaned source")
    else:
        value = json.loads(receipt.read_text())
        value["action_evidence"][0]["after"]["sha256"] = "0" * 64
        receipt.chmod(0o600)
        receipt.write_text(json.dumps(value))
    before = fingerprint(store.root)
    with pytest.raises(ControllerError):
        lifecycle.release(repository.repo, target=worktree, expected_record_version=version,
            owner_release="changed" if drift == "owner" else "module-owner", dry_run=True)
    assert worktree.exists() and fingerprint(store.root) == before


@pytest.mark.parametrize("target", ["retained-feature", "retained-audit"], indirect=True)
def test_two_generations_clean_then_exact_archive_release_retains_branches(exact_generations):
    repository, worktree, allocation, copied = exact_generations
    store = MetadataStore(inventory(repository.repo).common_dir)
    before_record = store.load().records[allocation]
    before_history = fingerprint(store.root)
    head = git(worktree, "rev-parse", "HEAD")
    branch = git(worktree, "branch", "--show-current")
    refs = git(repository.repo, "for-each-ref", "--format=%(refname) %(objectname)")
    index = Path(git(worktree, "rev-parse", "--git-path", "index"))
    index_sha = hashlib.sha256(index.read_bytes()).hexdigest()
    receipts = {path: path.read_bytes() for path, _sha in copied}

    planned = _cleanup(repository, worktree, copied)
    assert not planned["plan"]["blockers"]
    assert set(planned["plan"]["cleanup_paths"]) == set(RESOURCE_TYPES) | {".venv", "unknown"}
    assert fingerprint(store.root) == before_history
    cleaned = _cleanup(repository, worktree, copied, plan_id=planned["plan"]["plan_id"])
    assert cleaned["allocation_released"] is False
    _path, completion = _receipt(store, cleaned["receipt_id"])
    assert completion["result"] == "CLEANED"
    assert all(action["status"] == "COMPLETED" for action in completion["actions"])
    assert completion["post_scan"]["blockers"] == completion["post_scan"]["cleanup_paths"] == []
    assert store.load().records[allocation] == before_record
    assert worktree.exists()
    assert git(worktree, "rev-parse", "HEAD") == head
    assert git(repository.repo, "for-each-ref", "--format=%(refname) %(objectname)") == refs
    assert hashlib.sha256(index.read_bytes()).hexdigest() == index_sha
    for relative, digest in before_history.items():
        assert hashlib.sha256((store.root / relative).read_bytes()).hexdigest() == digest

    archived = _archive(repository, worktree)
    assert archived["decision"]["task_completed"] is False
    assert archived["decision"]["record_version"] == 3
    lifecycle.release(
        repository.repo, target=worktree, expected_record_version=3, owner_release="module-owner"
    )
    assert not worktree.exists()
    assert git(repository.repo, "rev-parse", branch) == head
    assert store.load().records[allocation].lifecycle == "RELEASED"
    for path, raw in receipts.items():
        assert path.read_bytes() == raw
    for path, sha in copied:
        assert pc.coverage(path, sha, verify_source=False)[0]["receipt_sha256"] == sha


@pytest.mark.parametrize("target", ["retained-feature"], indirect=True)
@pytest.mark.parametrize(
    "mutation", ["missing", "receipt-bytes", "manifest-bytes", "duplicate", "uncovered"]
)
def test_one_invalid_or_uncovered_resource_blocks_every_action(exact_generations, mutation):
    repository, worktree, allocation, copied = exact_generations
    store = MetadataStore(inventory(repository.repo).common_dir)
    before_record = store.load().records[allocation]
    before = fingerprint(worktree)
    coverage = copied
    second, _sha = copied[1]
    if mutation == "missing":
        coverage = (copied[0], (second.with_name("missing-receipt.json"), copied[1][1]))
    elif mutation in {"receipt-bytes", "manifest-bytes"}:
        changed = second if mutation == "receipt-bytes" else second.parent / "manifest.json"
        changed.chmod(0o600)
        changed.write_bytes(changed.read_bytes() + b" ")
    elif mutation == "duplicate":
        coverage = (copied[0], copied[0], copied[1])
    else:
        (worktree / "frontend/dist/theme-bootstrap.js").write_bytes(b"uncovered historical output")
        before = fingerprint(worktree)
    _assert_blocked(_cleanup(repository, worktree, coverage))
    assert store.load().records[allocation] == before_record
    assert fingerprint(worktree) == before
    assert all(
        os.path.lexists(worktree / relative) for relative in (*RESOURCE_TYPES, ".venv", "unknown")
    )
    assert not list(store.transactions_dir.glob("*.json"))


@pytest.mark.parametrize("target", ["retained-feature"], indirect=True)
def test_missing_second_generation_cannot_authorize_subset_cleanup(exact_generations):
    repository, worktree, _allocation, copied = exact_generations
    _assert_blocked(_cleanup(repository, worktree, copied[:1]))
    assert (worktree / "unknown/state.bin").read_bytes() == b"second opaque fixture\x00\xfe"
    assert (worktree / ".venv/state.bin").read_bytes() == b"first opaque fixture\x00\xff"
    assert all((worktree / relative).is_dir() for relative in RESOURCE_TYPES)


@pytest.mark.parametrize("target", ["retained-feature"], indirect=True)
def test_parent_and_leaf_receipts_cannot_overlap(exact_generations):
    repository, worktree, allocation, copied = exact_generations
    leaf = _preserve(repository, worktree, allocation, "unknown/state.bin")
    _assert_blocked(_cleanup(repository, worktree, (*copied, leaf)))
    assert (worktree / "unknown/state.bin").is_file()
    assert all((worktree / relative).is_dir() for relative in RESOURCE_TYPES)


@pytest.mark.parametrize("target", ["retained-feature"], indirect=True)
def test_additional_receipt_flags_require_exact_pairs(exact_generations):
    repository, worktree, _allocation, copied = exact_generations
    before = fingerprint(worktree)
    decision = lifecycle.cleanup_typed_disposable(
        repository.repo,
        target=worktree,
        expected_record_version=2,
        owner="module-owner",
        dry_run=True,
        preservation_receipt=copied[0][0],
        preservation_receipt_sha256=copied[0][1],
        additional_preservation_receipts=(copied[1][0],),
    )["decision"]
    _assert_blocked(decision)
    assert fingerprint(worktree) == before


@pytest.mark.parametrize("target", ["retained-feature"], indirect=True)
@pytest.mark.parametrize("condition", ["runtime", "uncertain", "writer", "binding"])
def test_generations_do_not_override_live_authority_guards(
    exact_generations, monkeypatch, condition
):
    repository, worktree, allocation, copied = exact_generations
    store = MetadataStore(inventory(repository.repo).common_dir)
    record = store.load().records[allocation]
    if condition in {"runtime", "uncertain"}:
        evidence = empty_runtime_evidence()
        key = (
            "observed_references" if condition == "runtime" else "managed_visibility_uncertainties"
        )
        evidence[key].append({"kind": "cwd", "pid": 12345})
        monkeypatch.setattr(lifecycle, "runtime_authority_evidence", lambda *a, **kw: evidence)
    elif condition == "writer":
        store.write_writer_lease(
            new_lease(
                repository_id=store.load().repository_id,
                allocation_id=allocation,
                worktree_path=str(worktree),
                owner=record.owner,
                holder=describe_holder(os.getpid(), client_id="manual"),
                ttl_seconds=None,
            )
        )
    else:
        store.replace_record(replace(record, external_bindings=("synthetic-live-session",)))
    before = fingerprint(worktree)
    decision = _cleanup(repository, worktree, copied)
    _assert_blocked(decision)
    expected = {
        "runtime": "RUNTIME_REFERENCE",
        "uncertain": "RUNTIME_SCAN_UNCERTAIN",
        "writer": "MANAGED_REFERENCE",
        "binding": "EXTERNAL_BINDING",
    }[condition]
    assert expected in {blocker["code"] for blocker in decision["plan"]["blockers"]}
    assert fingerprint(worktree) == before


@pytest.mark.parametrize("target", ["retained-feature"], indirect=True)
def test_regular_leaf_cleanup_keeps_tracked_parent_and_other_tracked_files(
    target, tmp_path, monkeypatch
):
    repository, worktree, allocation = target
    parent = worktree / "desktop/build"
    parent.mkdir(parents=True)
    (parent / "keep.txt").write_bytes(b"tracked build source")
    (worktree / ".gitignore").write_text(
        (worktree / ".gitignore").read_text() + "desktop/build/release-info.json\n"
    )
    git(worktree, "add", ".gitignore", "desktop/build/keep.txt")
    git(worktree, "commit", "-m", "synthetic tracked parent identity")
    leaf = parent / "release-info.json"
    leaf.write_bytes(b'{"sourceOid":"synthetic","dirtyWorktree":true}\n')
    monkeypatch.setattr(p, "evidence_root", lambda: tmp_path / "preservations")
    copied = (_preserve(repository, worktree, allocation, "desktop/build/release-info.json"),)
    head = git(worktree, "rev-parse", "HEAD")
    planned = _cleanup(repository, worktree, copied)
    assert not planned["plan"]["blockers"]
    _cleanup(repository, worktree, copied, plan_id=planned["plan"]["plan_id"])
    assert not os.path.lexists(leaf)
    assert parent.is_dir()
    assert (parent / "keep.txt").read_bytes() == b"tracked build source"
    assert git(worktree, "rev-parse", "HEAD") == head
    assert git(worktree, "status", "--porcelain") == ""
    with pytest.raises(ControllerError):
        p.plan_preservation(
            repository.repo,
            worktree,
            allocation,
            "module-owner",
            2,
            source_relative="desktop/build",
        )


@pytest.mark.parametrize("target", ["retained-feature"], indirect=True)
def test_root_symlink_is_preserved_as_literal_and_only_link_is_unlinked(
    target, tmp_path, monkeypatch
):
    repository, worktree, allocation = target
    (worktree / ".gitignore").write_text(
        (worktree / ".gitignore").read_text() + "frontend/node_modules\n"
    )
    git(worktree, "add", ".gitignore")
    git(worktree, "commit", "-m", "synthetic ignored link identity")
    link = worktree / "frontend/node_modules"
    shutil.rmtree(link)
    outside = tmp_path / "canonical-node-modules"
    outside.mkdir()
    sentinel = outside / "sentinel"
    sentinel.write_bytes(b"canonical environment remains untouched")
    link.symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr(p, "evidence_root", lambda: tmp_path / "preservations")
    original = os.open

    def no_target_io(name, flags, *args, **kwargs):
        assert not str(name).startswith(str(outside))
        return original(name, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", no_target_io)
    copied = (_preserve(repository, worktree, allocation, "frontend/node_modules"),)
    planned = _cleanup(repository, worktree, copied)
    assert not planned["plan"]["blockers"]
    _cleanup(repository, worktree, copied, plan_id=planned["plan"]["plan_id"])
    assert not os.path.lexists(link)
    assert outside.is_dir()
    assert sentinel.read_bytes() == b"canonical environment remains untouched"
    assert pc.coverage(*copied[0], verify_source=False)[0]["receipt_sha256"] == copied[0][1]


@pytest.mark.parametrize("target", ["retained-feature"], indirect=True)
def test_cross_generation_hardlink_decrements_are_exact(target, tmp_path, monkeypatch):
    repository, worktree, allocation = target
    (worktree / ".venv").mkdir()
    (worktree / "unknown").mkdir()
    first = worktree / ".venv/shared.bin"
    second = worktree / "unknown/shared.bin"
    first.write_bytes(b"opaque shared fixture")
    os.link(first, second)
    monkeypatch.setattr(p, "evidence_root", lambda: tmp_path / "preservations")
    copied = tuple(
        _preserve(repository, worktree, allocation, relative) for relative in (".venv", "unknown")
    )
    planned = _cleanup(repository, worktree, copied)
    assert not planned["plan"]["blockers"]
    cleaned = _cleanup(repository, worktree, copied, plan_id=planned["plan"]["plan_id"])
    store = MetadataStore(inventory(repository.repo).common_dir)
    _path, completion = _receipt(store, cleaned["receipt_id"])
    assert completion["result"] == "CLEANED"
    assert not os.path.lexists(first) and not os.path.lexists(second)
    assert len(completion["action_evidence"]) == len(completion["actions"])
    for path, sha in copied:
        pc.coverage(path, sha, verify_source=False)


@pytest.mark.parametrize("target", ["retained-feature"], indirect=True)
def test_partial_composition_keeps_journal_and_never_replays_completed_source(
    exact_generations, monkeypatch
):
    repository, worktree, _allocation, copied = exact_generations
    planned = _cleanup(repository, worktree, copied)
    assert not planned["plan"]["blockers"]
    original = lifecycle.delete_preserved_state
    count = 0

    def fail_second(root, evidence):
        nonlocal count
        count += 1
        if count == 2:
            raise ControllerError(
                "SYNTHETIC_DELETE_FAILURE", "second preservation action interrupted"
            )
        return original(root, evidence)

    monkeypatch.setattr(lifecycle, "delete_preserved_state", fail_second)
    with pytest.raises(ControllerError):
        _cleanup(repository, worktree, copied, plan_id=planned["plan"]["plan_id"])
    store = MetadataStore(inventory(repository.repo).common_dir)
    journals = list(store.transactions_dir.glob("*.json"))
    assert len(journals) == 1
    journal = json.loads(journals[0].read_text())
    _path, completion = _receipt(store, journal["transaction_id"])
    assert completion["result"] == "FAILED_OR_PARTIAL"
    preserved_actions = [
        action for action in completion["actions"] if action["path"] in {".venv", "unknown"}
    ]
    assert [action["status"] for action in preserved_actions] == ["COMPLETED", "FAILED_OR_PARTIAL"]
    completed = worktree / preserved_actions[0]["path"]
    pending = worktree / preserved_actions[1]["path"]
    assert not os.path.lexists(completed) and os.path.lexists(pending)
    journal_bytes = journals[0].read_bytes()
    monkeypatch.setattr(lifecycle, "delete_preserved_state", original)
    _assert_blocked(_cleanup(repository, worktree, copied))
    assert journals[0].read_bytes() == journal_bytes
    assert os.path.lexists(pending)
    assert count == 2


@pytest.mark.parametrize("target", ["retained-feature"], indirect=True)
@pytest.mark.parametrize("mutation", ["cas-plus-two", "generation", "completion-bytes"])
def test_archive_release_rejects_nonexact_cas_or_changed_immutable_evidence(
    exact_generations, mutation
):
    repository, worktree, allocation, copied = exact_generations
    planned = _cleanup(repository, worktree, copied)
    assert not planned["plan"]["blockers"]
    cleaned = _cleanup(repository, worktree, copied, plan_id=planned["plan"]["plan_id"])
    store = MetadataStore(inventory(repository.repo).common_dir)
    completion_path, _completion = _receipt(store, cleaned["receipt_id"])
    _archive(repository, worktree)
    record = store.load().records[allocation]
    assert record.record_version == 3
    head = git(worktree, "rev-parse", "HEAD")
    branch = git(worktree, "branch", "--show-current")
    if mutation == "cas-plus-two":
        store.replace_record(replace(record, record_version=4))
    elif mutation == "generation":
        node = copied[0][0].parent / "tree/state.bin"
        node.chmod(0o600)
        node.write_bytes(b"changed preserved generation")
    else:
        completion_path.write_bytes(completion_path.read_bytes() + b" ")
    with pytest.raises(ControllerError):
        lifecycle.release(
            repository.repo,
            target=worktree,
            expected_record_version=4 if mutation == "cas-plus-two" else 3,
            owner_release="module-owner",
        )
    assert worktree.exists()
    assert git(repository.repo, "rev-parse", branch) == head
    assert store.load().records[allocation].lifecycle == "ARCHIVED"


@pytest.fixture
def cleaned_exact(exact_generations):
    repository, worktree, allocation, copied = exact_generations
    planned = _cleanup(repository, worktree, copied)
    assert planned["plan"]["blockers"] == []
    cleaned = _cleanup(repository, worktree, copied, plan_id=planned["plan"]["plan_id"])
    store = MetadataStore(inventory(repository.repo).common_dir)
    path, receipt = _receipt(store, cleaned["receipt_id"])
    return repository, worktree, allocation, copied, store, path, receipt


@pytest.mark.parametrize("target", ["retained-feature"], indirect=True)
def test_completion_references_are_frozen_before_archive(cleaned_exact):
    repository, worktree, allocation, _copied, store, path, receipt = cleaned_exact
    assert store.load().records[allocation].record_version == 2
    with pytest.raises(ControllerError, match="not releasable"):
        lifecycle.release(
            repository.repo,
            target=worktree,
            expected_record_version=2,
            owner_release="module-owner",
            dry_run=True,
        )
    archived = _archive(repository, worktree)
    _archive_path, closed = _receipt(store, archived["decision"]["receipt_id"])
    assert closed["plan"]["preservation_cleanup_receipts"] == [
        {
            "receipt_id": receipt["receipt_id"],
            "path": str(path),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
    ]
    assert len(receipt["plan"]["preservation_evidences"]) == 2


@pytest.mark.parametrize("target", ["retained-feature"], indirect=True)
@pytest.mark.parametrize("phase", ["before-archive", "after-archive"])
@pytest.mark.parametrize(
    "mutation",
    [
        "missing-completion",
        "missing-before",
        "after-bytes",
        "other-transaction",
        "first-graph",
        "last-graph",
    ],
)
def test_immutable_completion_and_action_chain_failure_blocks_archive_or_release(
    cleaned_exact, phase, mutation
):
    repository, worktree, allocation, _copied, store, path, receipt = cleaned_exact
    if phase == "after-archive":
        _archive(repository, worktree)
    if mutation == "missing-completion":
        path.unlink()
    elif mutation == "missing-before":
        Path(receipt["action_evidence"][0]["before"]["path"]).unlink()
    elif mutation == "after-bytes":
        changed = Path(receipt["action_evidence"][0]["after"]["path"])
        changed.chmod(0o600)
        changed.write_bytes(changed.read_bytes() + b" ")
    elif mutation == "other-transaction":
        before = receipt["action_evidence"][0]["before"]
        before["path"] = str(
            Path(before["path"]).parent.with_name("00000000-0000-4000-8000-000000000000")
            / "0000-before.json"
        )
        path.write_bytes(p._json_bytes(receipt))
    elif mutation == "first-graph":
        graph = receipt["plan"]["hardlink_topology"]
        key = next(iter(graph["groups"]))
        graph["groups"][key]["content"]["content_sha256"] = "0" * 64
        graph["sha256"] = p._sha({k: v for k, v in graph.items() if k != "sha256"})
        receipt["plan"]["plan_id"] = lifecycle._release_plan_id(
            {k: v for k, v in receipt["plan"].items() if k != "plan_id"}
        )
        path.write_bytes(p._json_bytes(receipt))
    else:
        graph = receipt["post_scan"]["hardlink_topology"]
        graph["resources"] = ["forged-empty-resource"]
        graph["sha256"] = p._sha({k: v for k, v in graph.items() if k != "sha256"})
        receipt["post_scan"]["plan_id"] = lifecycle._release_plan_id(
            {k: v for k, v in receipt["post_scan"].items() if k != "plan_id"}
        )
        path.write_bytes(p._json_bytes(receipt))
    before = fingerprint(store.root)
    with pytest.raises(ControllerError):
        if phase == "before-archive":
            archive_retained(
                repository.repo,
                target=worktree,
                expected_record_version=2,
                owner_release="module-owner",
                actor="governance",
                dry_run=True,
            )
        else:
            lifecycle.release(
                repository.repo,
                target=worktree,
                expected_record_version=3,
                owner_release="module-owner",
                dry_run=True,
            )
    assert worktree.exists()
    assert fingerprint(store.root) == before
    assert store.load().records[allocation].record_version == (3 if phase == "after-archive" else 2)


@pytest.mark.parametrize("target", ["retained-feature"], indirect=True)
@pytest.mark.parametrize("mutation", ["owner", "authority", "head", "event"])
def test_archived_cleanup_never_inherits_changed_identity(cleaned_exact, mutation):
    repository, worktree, allocation, _copied, store, _path, _receipt_value = cleaned_exact
    _archive(repository, worktree)
    record = store.load().records[allocation]
    owner = record.owner
    if mutation == "owner":
        owner = "new-owner"
        store.replace_record(replace(record, owner=owner))
    elif mutation == "authority":
        store.replace_record(replace(record, authority_key="another-task-authority"))
    elif mutation == "head":
        (worktree / "identity-drift.txt").write_text("synthetic checkout drift")
        git(worktree, "add", "identity-drift.txt")
        git(worktree, "commit", "-m", "synthetic identity drift")
    else:
        event = {**record.history[-1], "event": "OWNER_TRANSFER"}
        store.replace_record(replace(record, history=(*record.history[:-1], event)))
    with pytest.raises(ControllerError):
        lifecycle.release(
            repository.repo,
            target=worktree,
            expected_record_version=3,
            owner_release=owner,
            dry_run=True,
        )
    assert worktree.exists()


@pytest.mark.parametrize("target", ["retained-feature"], indirect=True)
@pytest.mark.parametrize("root_witness", [False, True])
def test_old_nested_generation_requires_independent_original_root_witness(
    target, tmp_path, monkeypatch, root_witness
):
    repository, worktree, allocation = target
    monkeypatch.setattr(p, "evidence_root", lambda: tmp_path / "preservations")
    (worktree / ".venv").mkdir()
    (worktree / ".venv/opaque.bin").write_bytes(b"original root witness")
    old = p.plan_preservation(
        repository.repo, worktree, allocation, "module-owner", 2, source_relative="backend/.venv"
    )
    old.pop("source_ancestor_identities")
    old["plan_sha256"] = p._sha({k: v for k, v in old.items() if k != "plan_sha256"})
    result = p.copy_preservation(old)
    nested = (Path(result["receipt"]), result["receipt_sha256"])
    copied = (nested,)
    if root_witness:
        copied = (_preserve(repository, worktree, allocation, ".venv"), nested)
    decision = _cleanup(repository, worktree, copied)
    if not root_witness:
        _assert_blocked(decision)
        assert (worktree / "backend/.venv").is_dir()
    else:
        assert decision["plan"]["blockers"] == []
        _cleanup(repository, worktree, copied, plan_id=decision["plan"]["plan_id"])
        _archive(repository, worktree)
        lifecycle.release(
            repository.repo,
            target=worktree,
            expected_record_version=3,
            owner_release="module-owner",
        )
        assert not worktree.exists()


@pytest.mark.parametrize("target", ["retained-feature"], indirect=True)
@pytest.mark.parametrize("mutation", ["typed-proof", "typed-row", "post-contract"])
def test_single_proof_cannot_downgrade_to_typed_or_change_post_contract(
    target, tmp_path, monkeypatch, mutation
):
    repository, worktree, allocation = target
    monkeypatch.setattr(p, "evidence_root", lambda: tmp_path / "preservations")
    (worktree / ".venv").mkdir()
    (worktree / ".venv/state.bin").write_bytes(b"single opaque fixture")
    copied = (_preserve(repository, worktree, allocation, ".venv"),)
    planned = _cleanup(repository, worktree, copied)
    assert not planned["plan"]["blockers"]
    cleaned = _cleanup(repository, worktree, copied, plan_id=planned["plan"]["plan_id"])
    store = MetadataStore(inventory(repository.repo).common_dir)
    path, receipt = _receipt(store, cleaned["receipt_id"])
    if mutation.startswith("typed"):
        receipt["contract"] = lifecycle.TYPED_RESOURCE_CONTRACT
        receipt["plan"]["contract"] = lifecycle.TYPED_RESOURCE_CONTRACT
        receipt["post_scan"]["contract"] = lifecycle.TYPED_RESOURCE_CONTRACT
        if mutation == "typed-row":
            receipt["plan"].pop("preservation_evidence")
            receipt["post_scan"].pop("preservation_evidence")
        changed = copied[0][0].parent / "tree/state.bin"
        changed.chmod(0o600)
        changed.write_bytes(b"generation changed after a single-proof downgrade")
    else:
        receipt["post_scan"]["contract"] = lifecycle.TYPED_RESOURCE_CONTRACT
    for key in ("plan", "post_scan"):
        receipt[key]["plan_id"] = lifecycle._release_plan_id(
            {k: v for k, v in receipt[key].items() if k != "plan_id"}
        )
    path.write_bytes(p._json_bytes(receipt))
    before = fingerprint(store.root)
    with pytest.raises(ControllerError):
        archive_retained(
            repository.repo,
            target=worktree,
            expected_record_version=2,
            owner_release="module-owner",
            actor="governance",
            dry_run=True,
        )
    assert worktree.exists()
    assert fingerprint(store.root) == before


@pytest.mark.parametrize("target", ["retained-feature"], indirect=True)
@pytest.mark.parametrize("mutation", [None, "family-pair", "source-family-downgrade"])
def test_repeated_generation_uses_original_source_proof_and_strict_family_metadata(
    exact_generations, monkeypatch, mutation
):
    repository, worktree, allocation, copied = exact_generations
    original_authority = p._authority

    def current_observation(*args, **kwargs):
        authority = original_authority(*args, **kwargs)
        return {
            **authority,
            "warnings": [
                *authority["warnings"],
                {
                    "code": "HOST_VISIBILITY_GAP",
                    "message": "synthetic later observation",
                },
            ],
        }

    monkeypatch.setattr(p, "_authority", current_observation)
    repeated, _sha = _preserve(repository, worktree, allocation, ".venv")
    original_plan = json.loads((copied[0][0].parent / "plan.json").read_text())
    repeat_plan = json.loads((repeated.parent / "plan.json").read_text())
    assert repeat_plan["authority"] != original_plan["authority"]
    assert repeat_plan["authority"]["identity"] == original_plan["authority"]["identity"]
    assert repeat_plan["source_tree"] == original_plan["source_tree"]
    planned = _cleanup(repository, worktree, copied)
    assert not planned["plan"]["blockers"]
    _cleanup(repository, worktree, copied, plan_id=planned["plan"]["plan_id"])

    if mutation:
        repeat_receipt = json.loads(repeated.read_text())
        repeat_receipt["contract"] = "tracked-source-preservation/v1"
        if mutation == "source-family-downgrade":
            repeat_plan["contract"] = repeat_receipt["contract"]
            repeat_receipt["result"] = "SOURCE_PRESERVATION_COMPLETE"
            repeat_plan["plan_sha256"] = p._sha(
                {k: v for k, v in repeat_plan.items() if k != "plan_sha256"}
            )
            repeat_receipt["plan_sha256"] = repeat_plan["plan_sha256"]
            plan_path = repeated.parent / "plan.json"
            plan_path.chmod(0o600)
            plan_path.write_bytes(p._json_bytes(repeat_plan))
        repeated.chmod(0o600)
        repeated.write_bytes(p._json_bytes(repeat_receipt))
        store = MetadataStore(inventory(repository.repo).common_dir)
        before = fingerprint(store.root)
        with pytest.raises(ControllerError):
            archive_retained(
                repository.repo,
                target=worktree,
                expected_record_version=2,
                owner_release="module-owner",
                actor="governance",
                dry_run=True,
            )
        assert fingerprint(store.root) == before
        assert store.load().records[allocation].lifecycle == "RETAINED"
        assert worktree.exists()
    else:
        raw = repeated.read_bytes()
        _archive(repository, worktree)
        lifecycle.release(
            repository.repo,
            target=worktree,
            expected_record_version=3,
            owner_release="module-owner",
        )
        assert repeated.read_bytes() == raw
        assert not worktree.exists()


@pytest.mark.parametrize("target", ["retained-feature"], indirect=True)
@pytest.mark.parametrize(
    "field",
    [
        "repository_id",
        "allocation_id",
        "target_path",
        "expected_head",
        "expected_record_version",
        "phase",
    ],
)
def test_cleanup_rejects_journal_header_drift_before_every_resource_action(
    exact_generations, monkeypatch, field
):
    repository, worktree, _allocation, copied = exact_generations
    planned = _cleanup(repository, worktree, copied)
    assert not planned["plan"]["blockers"]
    write_transaction = MetadataStore.write_transaction
    altered = False

    def change_header(store, transaction, journal):
        nonlocal altered
        if journal["command"] == "resource-clean" and not altered:
            altered = True
            value = {
                "repository_id": "00000000-0000-4000-8000-000000000001",
                "allocation_id": "00000000-0000-4000-8000-000000000002",
                "target_path": str(worktree.with_name("another-synthetic-checkout")),
                "expected_head": "0" * 40,
                "expected_record_version": 3,
                "phase": "STARTED",
            }[field]
            journal = {**journal, field: value}
        return write_transaction(store, transaction, journal)

    monkeypatch.setattr(MetadataStore, "write_transaction", change_header)
    with pytest.raises(ControllerError) as refused:
        _cleanup(repository, worktree, copied, plan_id=planned["plan"]["plan_id"])
    assert altered
    assert refused.value.code == "PRESERVATION_COVERAGE_INVALID"
    assert all(
        os.path.lexists(worktree / relative) for relative in planned["plan"]["cleanup_paths"]
    )
    for path, sha in copied:
        assert hashlib.sha256(path.read_bytes()).hexdigest() == sha


@pytest.mark.parametrize("target", ["retained-feature"], indirect=True)
def test_renamed_generation_cannot_hide_missing_cleanup_completion(target, tmp_path, monkeypatch):
    repository, worktree, allocation = target
    monkeypatch.setattr(p, "evidence_root", lambda: tmp_path / "preservations")
    node = worktree / ".venv/state.bin"
    node.parent.mkdir()
    node.write_bytes(b"synthetic original preserved bytes")
    copied = (_preserve(repository, worktree, allocation, ".venv"),)
    planned = _cleanup(repository, worktree, copied)
    assert not planned["plan"]["blockers"]
    cleaned = _cleanup(repository, worktree, copied, plan_id=planned["plan"]["plan_id"])
    store = MetadataStore(inventory(repository.repo).common_dir)
    completion, _receipt_value = _receipt(store, cleaned["receipt_id"])
    completion.unlink()
    generation = copied[0][0].parent
    generation.rename(generation.with_name(generation.name + "-noncanonical"))
    before = fingerprint(store.root)
    with pytest.raises(ControllerError):
        archive_retained(
            repository.repo,
            target=worktree,
            expected_record_version=2,
            owner_release="module-owner",
            actor="governance",
            dry_run=True,
        )
    assert fingerprint(store.root) == before
    assert store.load().records[allocation].lifecycle == "RETAINED"
    assert worktree.exists()


@pytest.mark.parametrize("target", ["retained-feature"], indirect=True)
def test_new_completed_node_does_not_cover_previous_inode_at_same_path(exact_generations, tmp_path):
    repository, worktree, allocation, copied = exact_generations
    previous = tmp_path / "previous-source-node"
    (worktree / ".venv").rename(previous)
    (worktree / ".venv").mkdir()
    (worktree / ".venv/state.bin").write_bytes(b"new independently preserved fixture")
    current = (_preserve(repository, worktree, allocation, ".venv"), copied[1])
    planned = _cleanup(repository, worktree, current)
    assert not planned["plan"]["blockers"]
    with pytest.raises(ControllerError):
        _cleanup(repository, worktree, current, plan_id=planned["plan"]["plan_id"])
    assert (previous / "state.bin").read_bytes() == b"first opaque fixture\x00\xff"
    store = MetadataStore(inventory(repository.repo).common_dir)
    assert store.load().records[allocation].lifecycle == "RETAINED"
    assert store.load().records[allocation].record_version == 2
    assert list(store.transactions_dir.glob("*.json"))
    with pytest.raises(ControllerError):
        archive_retained(
            repository.repo,
            target=worktree,
            expected_record_version=2,
            owner_release="module-owner",
            actor="governance",
            dry_run=True,
        )


@pytest.mark.parametrize("target", ["retained-feature"], indirect=True)
def test_historical_generation_ancestor_drift_blocks_before_any_action(exact_generations):
    repository, worktree, allocation, copied = exact_generations
    current = (_preserve(repository, worktree, allocation, ".venv"), copied[1])
    old = copied[0][0]
    plan_path = old.parent / "plan.json"
    plan = json.loads(plan_path.read_text())
    receipt = json.loads(old.read_text())
    plan["source_ancestor_identities"][str(worktree)]["inode"] += 1
    plan["plan_sha256"] = p._sha({k: v for k, v in plan.items() if k != "plan_sha256"})
    receipt["plan_sha256"] = plan["plan_sha256"]
    plan_path.chmod(0o600)
    plan_path.write_bytes(p._json_bytes(plan))
    old.chmod(0o600)
    old.write_bytes(p._json_bytes(receipt))
    before = fingerprint(worktree)
    _assert_blocked(_cleanup(repository, worktree, current))
    assert fingerprint(worktree) == before
    store = MetadataStore(inventory(repository.repo).common_dir)
    assert store.load().records[allocation].record_version == 2
    assert not list(store.transactions_dir.glob("*.json"))


@pytest.mark.parametrize("pending_typed", ["refusal-receipt"], indirect=True)
@pytest.mark.parametrize("proof_shape", ["single", "multiple"])
def test_typed_no_action_refusal_cannot_hide_preservation_proof_shape(pending_typed, proof_shape):
    from worktree_controller import cleanup_attempts, cleanup_evidence

    repository, worktree, _allocation, store, journal_path, _digest, _options, _alias = (
        pending_typed
    )
    journal = json.loads(journal_path.read_text())
    receipt_path = store.completion_receipts_dir / journal_path.name
    receipt = json.loads(receipt_path.read_text())
    plan = journal["cleanup_plan"]
    if proof_shape == "single":
        plan["preservation_evidence"] = {"synthetic": "not cleanup authority"}
    else:
        plan["preservation_evidences"] = []
    plan["plan_id"] = lifecycle._release_plan_id({k: v for k, v in plan.items() if k != "plan_id"})
    receipt["plan"] = plan
    journal_path.write_bytes(p._json_bytes(journal))
    receipt_path.write_bytes(p._json_bytes(receipt))
    _journal, digest = cleanup_evidence.read(journal_path)
    before = fingerprint(worktree)
    original_journal = journal_path.read_bytes()
    original_receipt = receipt_path.read_bytes()
    with pytest.raises(ControllerError):
        cleanup_attempts.reconcile_typed(repository.repo, journal_path.stem, digest, dry_run=True)
    assert fingerprint(worktree) == before
    assert journal_path.read_bytes() == original_journal
    assert receipt_path.read_bytes() == original_receipt
    assert not (
        store.root / "cleanup-recoveries" / journal_path.stem / "typed-no-action.json"
    ).exists()
