from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from tests.test_typed_disposable import empty_runtime_evidence
from tests.test_typed_disposable import target as target
from worktree_controller import environment, lifecycle
from worktree_controller import preservation as p
from worktree_controller import preserved_cleanup as pc
from worktree_controller.git_backend import inventory
from worktree_controller.models import ControllerError
from worktree_controller.store import MetadataStore


@pytest.fixture
def preserved(target, tmp_path, monkeypatch):
    repository, worktree, allocation = target
    monkeypatch.setattr(lifecycle, "editable_references", environment.editable_references)
    state = worktree / ".venv/agent-workspace"
    state.mkdir(parents=True)
    (state / "unknown").write_bytes(b"opaque unknown state\x00\xff")
    (state / "legacy-cache.pth").write_text(str(worktree))
    os.link(state / "unknown", state / "hardlink")
    os.mkfifo(state / "pipe", 0o600)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "sentinel").write_bytes(b"external sentinel")
    (state / "external").symlink_to(outside)
    monkeypatch.setattr(p, "evidence_root", lambda: tmp_path / "preservations")
    plan = p.plan_preservation(repository.repo, worktree, allocation, "module-owner", 2)
    copied = p.copy_preservation(plan)
    yield repository, worktree, allocation, copied, outside


def dry_run(fixture, **kwargs):
    repository, worktree, _, copied, _ = fixture
    return lifecycle.cleanup_typed_disposable(
        repository.repo,
        target=worktree,
        expected_record_version=2,
        owner="module-owner",
        dry_run=True,
        preservation_receipt=Path(copied["receipt"]),
        preservation_receipt_sha256=copied["receipt_sha256"],
        **kwargs,
    )["decision"]


def clean(fixture, plan):
    repository, worktree, _, copied, _ = fixture
    return lifecycle.cleanup_typed_disposable(
        repository.repo,
        target=worktree,
        expected_record_version=2,
        owner="module-owner",
        dry_run=False,
        expected_plan_id=plan["plan_id"],
        preservation_receipt=Path(copied["receipt"]),
        preservation_receipt_sha256=copied["receipt_sha256"],
    )


def test_composed_cleanup_release_retains_generation_and_never_opens_fifo(preserved, monkeypatch):
    repository, worktree, allocation, copied, outside = preserved
    before_receipt = Path(copied["receipt"]).read_bytes()
    evidence = empty_runtime_evidence()
    evidence["host_visibility_gaps"] = [{"pid": 999999, "classification": "HOST_VISIBILITY_GAP"}]
    monkeypatch.setattr(lifecycle, "runtime_authority_evidence", lambda *args, **kwargs: evidence)
    plan = dry_run(preserved)
    assert plan["code"] == "CLEANUP_READY_WITH_PRESERVATION"
    assert plan["plan"]["cleanup_paths"] == [
        "backend/.venv",
        "frontend/dist",
        "frontend/node_modules",
        ".venv",
    ]
    rows = {r["path"]: r for r in plan["plan"]["resources"]}
    assert rows[".venv"]["classification"] == "exact-preserved"
    assert all(
        rows[path]["classification"] == "disposable" for path in plan["plan"]["cleanup_paths"][:-1]
    )
    original = os.open

    def no_fifo_io(name, flags, *args, **kwargs):
        assert str(name) != "pipe" or flags & os.O_PATH
        return original(name, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", no_fifo_io)
    result = clean(preserved, plan["plan"])
    assert result["decision"]["code"] == "PRESERVED_STATE_CLEANED"
    store = MetadataStore(inventory(repository.repo).common_dir)
    receipt = json.loads(
        (store.completion_receipts_dir / (result["decision"]["receipt_id"] + ".json")).read_text()
    )
    assert [a["status"] for a in receipt["actions"]] == ["COMPLETED"] * 4
    assert receipt["post_scan"]["blockers"] == receipt["post_scan"]["cleanup_paths"] == []
    assert (
        receipt["plan"]["warnings"]
        and receipt["runtime_checks"][-1]["runtime_evidence"]["host_visibility_gaps"]
    )
    assert store.load().records[allocation].lifecycle == "HANDOFF_READY"
    lifecycle.release(
        repository.repo, target=worktree, expected_record_version=2, owner_release="module-owner"
    )
    assert not worktree.exists()
    assert store.load().records[allocation].lifecycle == "RELEASED"
    assert list(store.transactions_dir.glob("*.json")) == []
    assert Path(copied["receipt"]).read_bytes() == before_receipt
    assert (
        pc.coverage(Path(copied["receipt"]), copied["receipt_sha256"], verify_source=False)[0][
            "tree_sha256"
        ]
        == copied["tree_sha256"]
    )
    assert (outside / "sentinel").read_bytes() == b"external sentinel"


@pytest.mark.parametrize(
    "mutation",
    [
        "wrong-allocation",
        "wrong-worktree",
        "wrong-source",
        "source-digest",
        "missing-generation",
        "manifest",
        "receipt-bytes",
        "incomplete-tree",
        "not-pass",
        "another-generation",
        "plan-bytes",
    ],
)
def test_exact_coverage_negative_cases_block(preserved, mutation):
    _, worktree, _, copied, _ = preserved
    receipt_path = Path(copied["receipt"])
    generation = receipt_path.parent
    receipt = json.loads(receipt_path.read_text())
    if mutation == "missing-generation":
        generation.rename(generation.with_name("saved-generation"))
    elif mutation == "manifest":
        path = generation / "manifest.json"
        path.chmod(0o600)
        path.write_bytes(path.read_bytes() + b" ")
    elif mutation == "plan-bytes":
        path = generation / "plan.json"
        plan = json.loads(path.read_text())
        plan["source"] = "/wrong/source"
        path.chmod(0o600)
        path.write_text(json.dumps(plan))
    elif mutation == "incomplete-tree":
        (generation / "tree/agent-workspace/unknown").unlink()
    elif mutation == "source-digest":
        (worktree / ".venv/agent-workspace/unknown").write_bytes(b"new unique state")
    else:
        if mutation == "wrong-allocation":
            receipt["allocation_source_identity"]["allocation_id"] = (
                "00000000-0000-4000-8000-000000000000"
            )
        elif mutation == "wrong-worktree":
            receipt["allocation_source_identity"]["target_path"] = "/wrong/worktree"
        elif mutation == "wrong-source":
            receipt["source"] = "/wrong/source"
        elif mutation == "not-pass":
            receipt["verification"] = "FAIL"
        elif mutation == "another-generation":
            receipt["receipt_id"] = "00000000-0000-4000-8000-000000000000"
        else:
            receipt["timestamp"] = "changed bytes"
        receipt_path.chmod(0o600)
        raw = p._json_bytes(receipt)
        receipt_path.write_bytes(raw)
        if mutation != "receipt-bytes":
            # Even an explicitly supplied hash cannot authorize wrong semantic bindings.
            copied["receipt_sha256"] = hashlib.sha256(raw).hexdigest()
    result = dry_run(preserved)
    assert result["code"] == "RESOURCE_CLEANUP_BLOCKED"
    assert result["plan"]["blockers"] and result["plan"]["cleanup_paths"] == []
    assert worktree.exists()


@pytest.mark.parametrize(
    "race", ["source-content", "parent-symlink", "leaf-symlink", "inode-replacement"]
)
def test_final_check_races_leave_external_sentinel_untouched(preserved, monkeypatch, race):
    _, worktree, _, copied, outside = preserved
    evidence, _ = pc.coverage(Path(copied["receipt"]), copied["receipt_sha256"])
    original = pc._delete_fd_tree

    def changed(*args, **kwargs):
        if race == "source-content":
            (worktree / ".venv/agent-workspace/unknown").write_bytes(b"changed after final scan")
        elif race == "parent-symlink":
            worktree.rename(worktree.with_name("original-worktree"))
            worktree.symlink_to(outside)
        else:
            source = worktree / ".venv"
            source.rename(worktree / "original-state")
            if race == "leaf-symlink":
                source.symlink_to(outside)
            else:
                source.mkdir()
        return original(*args, **kwargs)

    monkeypatch.setattr(pc, "_delete_fd_tree", changed)
    with pytest.raises((ControllerError, OSError)):
        pc.delete_preserved_state(worktree, evidence)
    assert (outside / "sentinel").read_bytes() == b"external sentinel"
    assert Path(copied["receipt"]).exists()


def test_partial_cleanup_failure_records_completed_and_pending_no_release(preserved, monkeypatch):
    repository, worktree, allocation, copied, _ = preserved
    decision = dry_run(preserved)
    original = lifecycle.delete_typed_resource

    def fail(root, relative, evidence):
        if relative == "frontend/dist":
            raise OSError(5, "synthetic deletion failure")
        return original(root, relative, evidence)

    monkeypatch.setattr(lifecycle, "delete_typed_resource", fail)
    with pytest.raises(ControllerError, match="immutable receipt"):
        clean(preserved, decision["plan"])
    store = MetadataStore(inventory(repository.repo).common_dir)
    receipts = [
        json.loads(path.read_text()) for path in store.completion_receipts_dir.glob("*.json")
    ]
    receipt = next(r for r in receipts if r.get("contract") == pc.CONTRACT)
    assert receipt["result"] == "FAILED_OR_PARTIAL"
    assert [a["status"] for a in receipt["actions"]] == [
        "COMPLETED",
        "FAILED_OR_PARTIAL",
        "PENDING",
        "PENDING",
    ]
    assert receipt["post_scan"]
    assert list(store.transactions_dir.glob("*.json"))
    with pytest.raises(ControllerError):
        lifecycle.release(
            repository.repo,
            target=worktree,
            expected_record_version=2,
            owner_release="module-owner",
        )
    assert store.load().records[allocation].lifecycle == "HANDOFF_READY"
    assert Path(copied["receipt"]).exists()


def test_generation_drift_after_cleanup_still_blocks_release(preserved):
    repository, worktree, _, copied, _ = preserved
    clean(preserved, dry_run(preserved)["plan"])
    (Path(copied["generation"]) / "tree/agent-workspace/unknown").write_bytes(
        b"corrupted preservation"
    )
    with pytest.raises(ControllerError):
        lifecycle.release(
            repository.repo,
            target=worktree,
            expected_record_version=2,
            owner_release="module-owner",
        )
    assert worktree.exists()


def test_reference_from_another_environment_still_blocks(preserved):
    repository, worktree, _, _, _ = preserved
    environment = repository.repo / ".venv"
    environment.mkdir()
    (environment / "linked-worktree.pth").write_text(str(worktree))
    result = dry_run(preserved)
    assert result["code"] == "RESOURCE_CLEANUP_BLOCKED"
    assert result["plan"]["cleanup_paths"] == []
    assert "STALE_EDITABLE_REFERENCE" in {b["code"] for b in result["plan"]["blockers"]}


def test_cli_exact_plan_renders_classification_and_host_gap(preserved, monkeypatch, capsys):
    from worktree_controller.cli import main

    repository, worktree, _, copied, _ = preserved
    evidence = empty_runtime_evidence()
    evidence["host_visibility_gaps"] = [{"pid": 999999, "classification": "HOST_VISIBILITY_GAP"}]
    monkeypatch.setattr(lifecycle, "runtime_authority_evidence", lambda *args, **kwargs: evidence)
    args = [
        "resource-clean",
        "--repo",
        str(repository.repo),
        "--path",
        str(worktree),
        "--owner",
        "module-owner",
        "--expected-record-version",
        "2",
        "--typed",
        "--dry-run",
        "--preservation-receipt",
        copied["receipt"],
        "--preservation-receipt-sha256",
        copied["receipt_sha256"],
    ]
    assert main([*args, "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["decision"]["code"] == "CLEANUP_READY_WITH_PRESERVATION"
    assert main(args) == 0
    rendered = capsys.readouterr().out
    assert "classification=exact-preserved" in rendered
    assert "Warning: HOST_VISIBILITY_GAP" in rendered
    assert "no claim of host-wide absence" in rendered


def test_destination_only_cli_verifies_after_release(preserved, capsys):
    repository, worktree, _, copied, _ = preserved
    clean(preserved, dry_run(preserved)["plan"])
    lifecycle.release(
        repository.repo, target=worktree, expected_record_version=2, owner_release="module-owner"
    )
    assert (
        p.main(
            [
                "verify",
                "--plan-file",
                str(Path(copied["generation"]) / "plan.json"),
                "--destination-only",
                "--receipt-sha256",
                copied["receipt_sha256"],
            ]
        )
        == 0
    )
    result = json.loads(capsys.readouterr().out)
    assert result["verification"] == "PASS"
    assert result["source_checked"] is False
    assert result["tree_sha256"] == copied["tree_sha256"]


@pytest.mark.parametrize("kind", ["unknown", "managed", "observed", "uncertain"])
def test_existing_safety_gates_cannot_be_overridden_by_receipt(preserved, monkeypatch, kind):
    _, worktree, _, _, _ = preserved
    if kind == "unknown":
        (worktree / "unknown").mkdir()
        (worktree / "unknown/state").write_bytes(b"unknown")
    else:
        evidence = empty_runtime_evidence()
        key = {
            "managed": "managed_references",
            "observed": "observed_references",
            "uncertain": "managed_visibility_uncertainties",
        }[kind]
        evidence[key] = [{"pid": 12345, "kind": "cwd"}]
        monkeypatch.setattr(
            lifecycle, "runtime_authority_evidence", lambda *args, **kwargs: evidence
        )
    result = dry_run(preserved)
    assert result["plan"]["blockers"] and result["plan"]["cleanup_paths"] == []


def test_subpath_preservation_and_cleanup(target, tmp_path, monkeypatch):
    repository, worktree, allocation = target
    monkeypatch.setattr(lifecycle, "editable_references", environment.editable_references)
    subpath = worktree / "backend/.venv"
    (subpath / "marker.txt").write_text("hello subpath")
    (subpath / "pytest-temp").mkdir(exist_ok=True)
    (subpath / "pytest-temp/junk").write_text("junk")
    monkeypatch.setattr(p, "evidence_root", lambda: tmp_path / "preservations")
    plan = p.plan_preservation(
        repository.repo,
        worktree,
        allocation,
        "module-owner",
        2,
        source_relative="backend/.venv",
    )
    copied = p.copy_preservation(plan)
    assert Path(copied["receipt"]).exists()

    evidence, verified_plan = pc.coverage(
        Path(copied["receipt"]),
        copied["receipt_sha256"],
        identity=plan["authority"]["identity"],
    )
    assert verified_plan["source_relative"] == "backend/.venv"
    assert subpath.exists()

    pc.delete_preserved_state(worktree, evidence)
    assert not subpath.exists()
    assert (worktree / "backend").exists()
