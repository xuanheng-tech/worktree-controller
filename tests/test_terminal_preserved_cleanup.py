"""Terminal cleanup preserves every tree and pins the original completion proof."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest

from tests.test_controlled import _configure_remote, acquire_one, adopt_canonical
from tests.test_integration_pipeline import _claim, _enqueue, _publish, _source, _validate
from tests.test_retention_admission import POLICY_V5
from tests.test_shadow import GitRepository, git
from tests.test_typed_disposable import empty_runtime_evidence
from worktree_controller import lifecycle
from worktree_controller import preservation as p
from worktree_controller.git_backend import inventory
from worktree_controller.models import ControllerError
from worktree_controller.store import MetadataStore


@pytest.fixture(params=["publication", "legacy_no_change", "no_change"])
def terminal(request, tmp_path, monkeypatch):
    monkeypatch.setattr(lifecycle, "runtime_references", lambda *a, **kw: ())
    monkeypatch.setattr(lifecycle, "editable_references", lambda *a, **kw: ())
    monkeypatch.setattr(lifecycle, "runtime_authority_evidence", lambda *a, **kw: empty_runtime_evidence())
    monkeypatch.setattr(p, "evidence_root", lambda: tmp_path / "preservations")
    with GitRepository(linked=0, policy_text=POLICY_V5) as repository:
        (repository.repo / ".gitignore").write_text("unknown/\n")
        git(repository.repo, "add", ".gitignore")
        git(repository.repo, "commit", "-m", "synthetic ignored state")
        adopt_canonical(repository)
        store = MetadataStore(inventory(repository.repo).common_dir)
        if request.param == "publication":
            _configure_remote(repository)
            _, allocation, _ = _source(repository, task="terminal-source", relative_path="source.txt", content="source\n")
            queued = _enqueue(repository, task="terminal-integration", source_allocation=allocation)
            target, _ = _claim(repository, task="terminal-integration", intent_id=queued["decision"]["intent_id"])
            _validate(repository, target, "terminal-validation")
            published = _publish(repository, target, auto_release=False)
            proof_id = published["decision"]["receipt_id"]
            record = store.load().records[store.load().publication_receipts[proof_id].allocation_id]
            proof = store.publication_receipts_dir / f"{proof_id}.json"
        else:
            target, acquired = acquire_one(repository, role="audit")
            allocation = acquired["decision"]["allocation_id"]
            result = lifecycle.complete_no_change(repository.repo, target=target, expected_record_version=1, owner="module-owner")
            proof = store.completion_receipts_dir / f"{result['decision']['completion_receipt_id']}.json"
            record = store.load().records[allocation]
            if request.param == "legacy_no_change":
                value = json.loads(proof.read_text())
                for key in ("contract", "before_record", "target_path", "preservation_cleanup_receipts", "completion_event"):
                    value.pop(key)
                proof.chmod(0o600)
                proof.write_text(json.dumps(value))
                event = dict(record.history[-1])
                event.pop("no_change_completion_ref")
                record = replace(record, history=(*record.history[:-1], event))
                store.replace_record(record)
        (target / "unknown").mkdir()
        (target / "unknown/result").write_bytes(b"opaque synthetic state")
        yield repository, target, store, record, proof


def _cleanup(state, copied=None, *, plan_id=None, **changes):
    repository, target, _, record, _ = state
    args = dict(target=target, expected_record_version=record.record_version, owner=record.owner,
                dry_run=plan_id is None, expected_plan_id=plan_id)
    if copied:
        args.update(preservation_receipt=Path(copied["receipt"]), preservation_receipt_sha256=copied["receipt_sha256"])
    args.update(changes)
    return lifecycle.cleanup_typed_disposable(repository.repo, **args)["decision"]


def _preserve(state):
    repository, target, _, record, _ = state
    return p.copy_preservation(p.plan_preservation(repository.repo, target, record.allocation_id,
        record.owner, record.record_version, source_relative="unknown"))


def test_terminal_preserve_cleanup_release_retains_proof_and_branch(terminal):
    repository, target, store, record, proof = terminal
    original = proof.read_bytes()
    blocked = _cleanup(terminal)
    assert blocked["plan"]["blockers"]
    assert (target / "unknown/result").read_bytes() == b"opaque synthetic state"
    copied = _preserve(terminal)
    planned = _cleanup(terminal, copied)
    assert not planned["plan"]["blockers"], planned
    assert planned["plan"]["terminal_evidence"]["kind"] in {"publication", "legacy_no_change", "no_change"}
    cleaned = _cleanup(terminal, copied, plan_id=planned["plan"]["plan_id"])
    assert cleaned["code"] == "PRESERVED_STATE_CLEANED"
    assert store.load().records[record.allocation_id] == record
    assert proof.read_bytes() == original
    assert not (target / "unknown").exists()
    head, branch = git(target, "rev-parse", "HEAD"), git(target, "branch", "--show-current")
    lifecycle.release(repository.repo, target=target, expected_record_version=record.record_version, owner_release=record.owner)
    assert not target.exists()
    assert git(repository.repo, "rev-parse", branch) == head
    assert Path(copied["receipt"]).is_file()
    assert store.load().records[record.allocation_id].lifecycle == "RELEASED"


@pytest.mark.parametrize("fault", ["missing", "proof-head", "owner", "cas", "dirty", "binding"])
def test_terminal_cleanup_guards_fail_without_mutating_source(terminal, fault):
    _, target, store, record, proof = terminal
    if fault == "missing":
        proof.unlink()
    elif fault == "proof-head":
        value = json.loads(proof.read_text())
        value["candidate_oid" if record.role == "integration" else "head"] = "0" * 40
        proof.chmod(0o600)
        proof.write_text(json.dumps(value))
    elif fault == "binding":
        store.replace_record(replace(record, external_bindings=("synthetic-binding",)))
    elif fault == "dirty":
        (target / "untracked-result").write_text("user work\n")
    changes = {"owner": "other-owner"} if fault == "owner" else {"expected_record_version": record.record_version + 1} if fault == "cas" else {}
    assert _cleanup(terminal, **changes)["plan"]["blockers"]
    assert (target / "unknown/result").read_bytes() == b"opaque synthetic state"


def test_terminal_proof_change_after_cleanup_blocks_release(terminal):
    repository, target, _, record, proof = terminal
    copied = _preserve(terminal)
    planned = _cleanup(terminal, copied)
    _cleanup(terminal, copied, plan_id=planned["plan"]["plan_id"])
    value = json.loads(proof.read_text())
    value["synthetic_metadata_change"] = "changed after reviewed cleanup"
    proof.chmod(0o600)
    proof.write_text(json.dumps(value))
    with pytest.raises(ControllerError):
        lifecycle.release(repository.repo, target=target, expected_record_version=record.record_version, owner_release=record.owner)
    assert target.exists()


@pytest.mark.parametrize("terminal", ["no_change"], indirect=True)
def test_small_terminal_preservation_does_not_redecode_unrelated_history(terminal, monkeypatch):
    from worktree_controller import cleanup_evidence, preserved_cleanup

    repository, target, store, record, proof = terminal
    original_proof = proof.read_bytes()
    small = target / "unknown/small"
    small.mkdir()
    for index, size in enumerate([5500, 5500, 5500, 5500, 5479]):
        (small / str(index)).write_bytes(b"s" * size)
    unrelated = set()
    for _ in range(3):
        path = store.completion_receipts_dir / f"{uuid4()}.json"
        store._atomic_json(path, {
            "contract": "legacy-preserved-state-cleanup/v1",
            "plan": {"allocation_id": str(uuid4())}, "opaque": "x" * 65536,
        })
        unrelated.add(path.name)
    reads = []
    original = cleanup_evidence.read_document

    def counted(directory, name):
        if name in unrelated:
            reads.append(name)
        return original(directory, name)

    monkeypatch.setattr(cleanup_evidence, "read_document", counted)
    monkeypatch.setattr(preserved_cleanup, "read_document", counted)
    planned = p.plan_preservation(
        repository.repo, target, record.allocation_id, record.owner, record.record_version,
        source_relative="unknown/small",
    )
    assert planned["ready"]
    assert planned["source_tree"]["inventory"]["regular_files"] == 5
    assert planned["source_tree"]["inventory"]["total_regular_file_bytes"] == 27479
    # Planning takes separate authority locks before and after scanning the source.
    # Each sees unrelated receipt bytes once, despite nested terminal checks.
    assert len(reads) == 2 * len(unrelated)
    assert proof.read_bytes() == original_proof
    assert store.load().records[record.allocation_id] == record
    assert (target / "unknown/result").read_bytes() == b"opaque synthetic state"
