"""An actual cleanup can supersede older copies across an exact reopened lineage."""

from __future__ import annotations

import json
import shutil
from contextlib import nullcontext
from copy import deepcopy
from dataclasses import replace
from pathlib import Path

import pytest

from tests.test_shadow import git
from tests.test_typed_disposable import target as target
from worktree_controller import lifecycle
from worktree_controller import preservation as p
from worktree_controller import preserved_cleanup as pc
from worktree_controller.git_backend import inventory
from worktree_controller.models import ControllerError
from worktree_controller.store import MetadataStore


def _copy(repository, worktree, allocation, version):
    plan = p.plan_preservation(
        repository.repo, worktree, allocation, "module-owner", version, source_relative=".venv"
    )
    result = p.copy_preservation(plan)
    return plan, Path(result["receipt"]), result["receipt_sha256"]


@pytest.fixture
def superseded(target, tmp_path, monkeypatch, request):
    repository, worktree, allocation = target
    monkeypatch.setattr(p, "evidence_root", lambda: tmp_path / "preservations")
    source = worktree / ".venv"
    source.mkdir()
    (source / "state.bin").write_bytes(b"old synthetic build state")
    old = _copy(repository, worktree, allocation, 2)
    store = MetadataStore(inventory(repository.repo).common_dir)
    mode = getattr(request, "param", "same-head")
    if mode == "no-reopen":
        lifecycle.review(
            repository.repo, target=worktree, expected_record_version=2, owner="module-owner"
        )
        version = 3
    else:
        lifecycle.reopen(
            repository.repo, target=worktree, expected_record_version=2, owner="module-owner"
        )
        version = 4
        if mode == "advanced-head":
            tracked = worktree / "backend/pyproject.toml"
            tracked.write_text(tracked.read_text() + "# synthetic rebuilt revision\n")
            git(worktree, "add", "backend/pyproject.toml")
            git(worktree, "commit", "-m", "synthetic successor revision")
        lifecycle.handoff(
            repository.repo,
            target=worktree,
            expected_record_version=3,
            kind="integration-ready",
            commit=git(worktree, "rev-parse", "HEAD"),
            finalizer_record_id="synthetic-successor-finalizer",
            scope_paths=("backend/pyproject.toml",) if mode == "advanced-head" else (),
        )
        if mode == "advanced-head":
            git(repository.repo, "merge", "--ff-only", git(worktree, "branch", "--show-current"))
    retired = tmp_path / "retired-source-node"
    source.rename(retired)
    source.mkdir()
    (source / "state.bin").write_bytes(b"new synthetic build state")
    current = _copy(repository, worktree, allocation, version)
    assert old[0]["source_anchor"]["source"] != current[0]["source_anchor"]["source"]
    options = dict(
        target=worktree,
        owner="module-owner",
        expected_record_version=version,
        preservation_receipt=current[1],
        preservation_receipt_sha256=current[2],
    )
    return repository, worktree, allocation, store, old, current, retired, options


def _clean(state):
    repository, _worktree, _allocation, _store, _old, _current, _retired, options = state
    planned = lifecycle.cleanup_typed_disposable(repository.repo, **options, dry_run=True)
    assert planned["decision"]["plan"]["blockers"] == []
    return lifecycle.cleanup_typed_disposable(
        repository.repo,
        **options,
        dry_run=False,
        expected_plan_id=planned["decision"]["plan"]["plan_id"],
    )


@pytest.mark.parametrize("superseded", ["same-head", "advanced-head"], indirect=True)
def test_formal_reopen_and_completed_cleanup_cover_old_rebuilt_source(superseded):
    repository, worktree, allocation, store, old, current, retired, options = superseded
    originals = {copy[1]: copy[1].read_bytes() for copy in (old, current)}
    result = _clean(superseded)
    assert result["decision"]["code"] == "PRESERVED_STATE_CLEANED"
    receipt_id = result["decision"]["receipt_id"]
    receipt = store._read_json(store.completion_receipts_dir / f"{receipt_id}.json")
    assert receipt["result"] == "CLEANED"
    assert all(row["status"] == "COMPLETED" for row in receipt["actions"])
    assert receipt["action_evidence"] and receipt["post_scan"]["blockers"] == []
    assert store.load().records[allocation].record_version == options["expected_record_version"]
    assert not (worktree / ".venv").exists()
    assert (retired / "state.bin").read_bytes() == b"old synthetic build state"
    assert all(path.read_bytes() == raw for path, raw in originals.items())
    lifecycle.release(
        repository.repo,
        target=worktree,
        expected_record_version=options["expected_record_version"],
        owner_release="module-owner",
    )
    assert not worktree.exists()
    assert all(path.read_bytes() == raw for path, raw in originals.items())


@pytest.mark.parametrize("superseded", ["no-reopen"], indirect=True)
def test_new_cas_without_reopen_cannot_supersede_previous_inode(superseded):
    _repository, worktree, allocation, store, old, _current, _retired, _options = superseded
    raw = old[1].read_bytes()
    with pytest.raises(ControllerError):
        _clean(superseded)
    assert old[1].read_bytes() == raw
    assert worktree.exists() and store.load().records[allocation].lifecycle == "HANDOFF_READY"
    assert list(store.transactions_dir.glob("*.json"))


@pytest.mark.parametrize(
    "drift", ["history-length", "history-chain", "reopen-kind", "owner", "head", "base"]
)
def test_supersession_rejects_unproven_history_and_lineage(superseded, drift):
    _repository, worktree, allocation, store, old, current, _retired, _options = superseded
    record = store.load().records[allocation]
    if drift == "history-length":
        record = replace(record, history=record.history[1:])
    elif drift in {"history-chain", "reopen-kind", "owner"}:
        event = dict(record.history[2])
        event.update({
            "history-chain": {"from": "RETAINED"},
            "reopen-kind": {"event": "REVIEWED"},
            "owner": {"owner": "another-synthetic-owner"},
        }[drift])
        record = replace(record, history=(*record.history[:2], event, *record.history[3:]))
    elif drift == "head":
        record = replace(record, handoff_commit="0" * 40)
    else:
        record = replace(record, base_commit="0" * 40)
    with pytest.raises(ControllerError) if drift == "base" else nullcontext():
        result = pc._same_completed_source(old[0], current[0], worktree, record=record)
        assert result is False


@pytest.mark.parametrize("drift", ["owner", "authority", "parent", "head-ancestry"])
def test_supersession_rejects_foreign_old_identity_and_divergent_head(superseded, drift):
    repository, worktree, allocation, store, old, current, _retired, _options = superseded
    previous = deepcopy(old[0])
    if drift == "parent":
        previous["source_anchor"]["worktree"]["inode"] += 1
    elif drift == "head-ancestry":
        identity = previous["authority"]["identity"]
        identity["expected_head"] = git(
            repository.repo,
            "commit-tree",
            git(repository.repo, "rev-parse", "HEAD^{tree}"),
            "-p",
            identity["expected_head"],
            "-m",
            "synthetic divergent historical head",
        )
    else:
        key = "owner" if drift == "owner" else "authority_key"
        previous["authority"]["identity"][key] = "another-synthetic-authority"
    assert not pc._same_completed_source(
        previous, current[0], worktree, record=store.load().records[allocation]
    )


def test_reopen_and_source_absence_without_action_evidence_still_block(superseded):
    repository, worktree, allocation, store, old, current, _retired, options = superseded
    shutil.rmtree(worktree / ".venv")
    with pytest.raises(ControllerError):
        lifecycle.release(
            repository.repo,
            target=worktree,
            expected_record_version=options["expected_record_version"],
            owner_release="module-owner",
            dry_run=True,
        )
    assert store.load().records[allocation].lifecycle == "HANDOFF_READY"
    assert old[1].exists() and current[1].exists()


def test_supersession_revalidates_historical_metadata_and_parent_pins(superseded):
    repository, worktree, _allocation, store, old, _current, _retired, _options = superseded
    _clean(superseded)
    plan_path = old[1].parent / "plan.json"
    plan = json.loads(plan_path.read_text())
    receipt = json.loads(old[1].read_text())
    plan["source_ancestor_identities"][str(worktree)]["inode"] += 1
    plan["plan_sha256"] = p._sha({k: v for k, v in plan.items() if k != "plan_sha256"})
    receipt["plan_sha256"] = plan["plan_sha256"]
    plan_path.chmod(0o600)
    plan_path.write_bytes(p._json_bytes(plan))
    old[1].chmod(0o600)
    old[1].write_bytes(p._json_bytes(receipt))
    with pytest.raises(ControllerError):
        lifecycle.release(
            repository.repo,
            target=worktree,
            expected_record_version=store.load().records[_allocation].record_version,
            owner_release="module-owner",
            dry_run=True,
        )
    assert worktree.exists()
