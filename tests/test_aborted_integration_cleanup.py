"""Failed integration budgets settle only after exact preservation and guarded release."""

from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest

from tests.test_controlled import _configure_remote, acquire_one, adopt_canonical, handoff_one
from tests.test_integration_abort import POLICY_THREE_CANDIDATES_V5
from tests.test_integration_pipeline import _claim, _source
from tests.test_shadow import GitRepository, git
from tests.test_typed_disposable import empty_runtime_evidence
from worktree_controller import lifecycle, preservation, resource_admission
from worktree_controller.git_backend import inventory
from worktree_controller.models import ControllerError
from worktree_controller.store import MetadataStore
from worktree_controller.writer_lease import new_lease, resolve_holder

POLICY = (
    POLICY_THREE_CANDIDATES_V5.replace("schema_version = 5", "schema_version = 8")
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
enabled = false
grace_minutes = 30
idle_hours_max = 72
idle_hours_min = 6
standing_reserve = 2
"""
)
PATHS = ("apps/qp_docs/.astro", "apps/qp_docs/.generated", "apps/qp_docs/dist")
OWNER = "integration-owner"


@pytest.fixture
def failed_candidate(tmp_path, monkeypatch):
    monkeypatch.setattr(
        lifecycle, "runtime_authority_evidence", lambda *a, **k: empty_runtime_evidence()
    )
    monkeypatch.setattr(lifecycle, "runtime_references", lambda *a, **k: ())
    monkeypatch.setattr(lifecycle, "editable_references", lambda *a, **k: ())
    monkeypatch.setattr(preservation, "evidence_root", lambda: tmp_path / "preservations")
    with GitRepository(linked=0, policy_text=POLICY) as repo:
        (repo.repo / ".gitignore").write_text("\n".join(p + "/" for p in PATHS) + "\n")
        git(repo.repo, "add", ".gitignore")
        git(repo.repo, "commit", "-m", "fixture generated paths")
        adopt_canonical(repo)
        _configure_remote(repo)
        sources = tuple(
            _source(repo, task=name, relative_path=f"src/{name}.css", content=name + "\n")
            for name in ("console", "docs")
        )
        intent = lifecycle.enqueue_integration(
            repo.repo,
            task_key="joint-old",
            authority_key="joint-authority",
            module="ui",
            owner=OWNER,
            source_allocations=tuple(row[1] for row in sources),
        )["decision"]
        candidate, claim = _claim(repo, task="joint-old", intent_id=intent["intent_id"])
        store = MetadataStore(inventory(repo.repo).common_dir)
        for relative in PATHS:
            path = candidate / relative
            path.mkdir(parents=True)
            (path / "opaque.bin").write_bytes(b"task evidence, preserve exactly\x00\xff")
        yield repo, candidate, claim["decision"], sources, store


def record(fixture):
    return fixture[4].load().records[fixture[2]["allocation_id"]]


def abort(fixture):
    repo, path, _, _, _ = fixture
    return lifecycle.abort_integration(
        repo.repo,
        target=path,
        expected_record_version=record(fixture).record_version,
        owner=OWNER,
        reason="real browser contrast failure",
    )["decision"]


def cleanup(fixture, proofs=(), **kwargs):
    repo, path, _, _, _ = fixture
    options = {}
    if proofs:
        options = {
            "preservation_receipt": Path(proofs[0]["receipt"]),
            "preservation_receipt_sha256": proofs[0]["receipt_sha256"],
            "additional_preservation_receipts": tuple(Path(p["receipt"]) for p in proofs[1:]),
            "additional_preservation_receipt_sha256": tuple(
                p["receipt_sha256"] for p in proofs[1:]
            ),
        }
    return lifecycle.cleanup_typed_disposable(
        repo.repo,
        target=path,
        expected_record_version=record(fixture).record_version,
        owner=OWNER,
        dry_run=True,
        **options,
        **kwargs,
    )["decision"]


def preserve(fixture, paths=PATHS):
    repo, path, claim, _, _ = fixture
    return tuple(
        preservation.copy_preservation(
            preservation.plan_preservation(
                repo.repo,
                path,
                claim["allocation_id"],
                OWNER,
                record(fixture).record_version,
                source_relative=relative,
            )
        )
        for relative in paths
    )


def test_exact_preserved_aborted_candidate_can_release_and_reapply(failed_candidate):
    f = failed_candidate
    repo, candidate, claim, sources, store = f
    before_head = git(candidate, "rev-parse", "HEAD")
    abort(f)
    lifecycle.abandon_integration_intent(
        repo.repo,
        intent_id=claim["intent_id"],
        owner=OWNER,
        reason="replace failed source with reviewed fix",
    )
    # Match Phase 1F: the old frozen candidate stays behind a later source handoff.
    source, allocation, _ = sources[1]
    lifecycle.reopen(repo.repo, target=source, expected_record_version=2, owner="module-owner")
    (source / "src/docs.css").write_text("fixed docs\n")
    git(source, "add", "src/docs.css")
    git(source, "commit", "-m", "fixture docs fix")
    handoff_one(repo, source, expected=3)
    corrected_head = git(source, "rev-parse", "HEAD")
    lifecycle.quarantine(
        repo.repo,
        target=candidate,
        expected_record_version=2,
        owner=OWNER,
        expected_commit=before_head,
        reason="frozen source advanced",
    )
    blocked = cleanup(f)
    assert "INVALID_LIFECYCLE" in {b["code"] for b in blocked["plan"]["blockers"]}
    # Only the existing, explicit owner/CAS abort transition can end quarantine.
    abort(f)
    pending = lifecycle.enqueue_integration(
        repo.repo,
        task_key="joint-corrected",
        authority_key="joint-authority",
        module="ui",
        owner=OWNER,
        source_allocations=tuple(row[1] for row in sources),
    )["decision"]
    with pytest.raises(ControllerError) as drift:
        _claim(repo, task="joint-corrected", intent_id=pending["intent_id"])
    assert drift.value.code == "ADMISSION_REQUEST_DRIFT"
    proofs = preserve(f)
    receipt_bytes = {p["receipt"]: Path(p["receipt"]).read_bytes() for p in proofs}
    plan = cleanup(f, proofs)
    assert plan["plan"]["blockers"] == []
    assert set(plan["plan"]["cleanup_paths"]) == set(PATHS)
    result = lifecycle.cleanup_typed_disposable(
        repo.repo,
        target=candidate,
        expected_record_version=record(f).record_version,
        owner=OWNER,
        dry_run=False,
        expected_plan_id=plan["plan"]["plan_id"],
        preservation_receipt=Path(proofs[0]["receipt"]),
        preservation_receipt_sha256=proofs[0]["receipt_sha256"],
        additional_preservation_receipts=tuple(Path(p["receipt"]) for p in proofs[1:]),
        additional_preservation_receipt_sha256=tuple(p["receipt_sha256"] for p in proofs[1:]),
    )["decision"]
    assert not result["allocation_released"]
    assert record(f).lifecycle == "INTEGRATION_ABORTED"
    # Consumed live-source proofs cannot be replayed. A fresh empty plan instead
    # verifies the completed immutable receipts and performs no second deletion.
    assert cleanup(f, proofs)["plan"]["blockers"]
    again = cleanup(f)
    assert again["plan"]["blockers"] == again["plan"]["cleanup_paths"] == []
    lifecycle.release(
        repo.repo,
        target=candidate,
        expected_record_version=record(f).record_version,
        owner_release=OWNER,
    )
    assert not candidate.exists()
    assert git(repo.repo, "rev-parse", "integration/joint-old") == before_head
    assert all(Path(p).read_bytes() == data for p, data in receipt_bytes.items())
    old_budget = [
        row
        for row in resource_admission.AdmissionLedger().read()["requests"].values()
        if row.get("allocation_id") == claim["allocation_id"]
    ]
    assert len(old_budget) == 1 and old_budget[0]["state"] == "RELEASED"
    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(
            pool.map(
                lambda _: _claim(repo, task="joint-corrected", intent_id=pending["intent_id"]),
                range(2),
            )
        )
    replacement, decision = claims[0]
    assert claims[0][1]["decision"]["allocation_id"] == claims[1][1]["decision"]["allocation_id"]
    assert decision["decision"]["code"] == "INTEGRATION_CANDIDATE_CLAIMED"
    assert git(replacement, "merge-base", "--is-ancestor", corrected_head, "HEAD") == ""
    assert store.load().records[allocation].handoff_commit == corrected_head


@pytest.mark.parametrize("missing", [0, 1, 2])
def test_omitted_preservation_cannot_clean_any_directory(failed_candidate, missing):
    f = failed_candidate
    abort(f)
    proofs = preserve(f)
    plan = cleanup(f, tuple(p for i, p in enumerate(proofs) if i != missing))
    assert plan["plan"]["blockers"]
    assert all((f[1] / relative / "opaque.bin").exists() for relative in PATHS)


def test_cleanup_preserves_dirty_user_file(failed_candidate):
    f = failed_candidate
    abort(f)
    proofs = preserve(f)
    note = f[1] / "user-notes.md"
    note.write_text("untracked user content\n")
    assert "DIRTY_WORKTREE" in {b["code"] for b in cleanup(f, proofs)["plan"]["blockers"]}
    assert note.read_text() == "untracked user content\n"
    assert all((f[1] / relative / "opaque.bin").exists() for relative in PATHS)


def test_abort_owner_and_cas_do_not_unlock_quarantine(failed_candidate):
    f = failed_candidate
    path = f[1]
    lifecycle.quarantine(
        f[0].repo,
        target=path,
        expected_record_version=1,
        owner=OWNER,
        expected_commit=git(path, "rev-parse", "HEAD"),
        reason="fixture quarantine",
    )
    for owner, version, code in [("foreign", 2, "OWNER_MISMATCH"), (OWNER, 1, "CAS_MISMATCH")]:
        with pytest.raises(ControllerError) as error:
            lifecycle.abort_integration(
                f[0].repo,
                target=path,
                expected_record_version=version,
                owner=owner,
                reason="unauthorized",
            )
        assert error.value.code == code
    assert record(f).lifecycle == "QUARANTINED"
    assert cleanup(f)["plan"]["blockers"]


def test_changed_request_identity_still_refused(failed_candidate):
    f = failed_candidate
    abort(f)
    for changed in ("owner", "branch", "target"):
        args = dict(
            task="unrelated",
            authority="joint-authority",
            role="integration",
            owner=OWNER,
            source_allocations=tuple(row[1] for row in f[3]),
        )
        if changed == "owner":
            args["owner"] = "foreign"
        if changed == "branch":
            args["task"] = "changed-branch"
        with pytest.raises(ControllerError) as error:
            acquire_one(f[0], **args)
        assert error.value.code == "ADMISSION_REQUEST_DRIFT"


def test_concurrent_dry_runs_do_not_mutate_failed_candidate(failed_candidate):
    f = failed_candidate
    abort(f)
    proofs = preserve(f)
    before = record(f)
    with ThreadPoolExecutor(max_workers=2) as pool:
        plans = list(pool.map(lambda _: cleanup(f, proofs)["plan"], range(2)))
    assert plans[0]["plan_id"] == plans[1]["plan_id"]
    assert plans[0]["blockers"] == plans[1]["blockers"] == []
    assert before == record(f)


def test_abort_receipt_is_required_for_cleanup(failed_candidate):
    f = failed_candidate
    receipt = abort(f)["abort_receipt_id"]
    (f[4].completion_receipts_dir / f"{receipt}.json").unlink()
    plan = cleanup(f)["plan"]
    assert "INTEGRATION_ABORT_EVIDENCE_INVALID" in {b["code"] for b in plan["blockers"]}


def test_changed_abort_bytes_invalidate_preservation(failed_candidate):
    f = failed_candidate
    receipt = abort(f)["abort_receipt_id"]
    receipt_path = f[4].completion_receipts_dir / f"{receipt}.json"
    value = json.loads(receipt_path.read_text())
    value["failure_reason"] = "changed historical failure"
    receipt_path.write_text(json.dumps(value))
    with pytest.raises(ControllerError) as error:
        preserve(f)
    assert error.value.code == "INTEGRATION_ABORT_EVIDENCE_INVALID"
    assert all((f[1] / relative / "opaque.bin").exists() for relative in PATHS)


@pytest.mark.parametrize("expired", [False, True])
def test_a_recorded_writer_lease_is_not_inferred_safe_to_clear(failed_candidate, expired):
    f = failed_candidate
    abort(f)
    proofs = preserve(f)
    # Simulate a legacy recorded lease on this now-fenced integration allocation.
    # Neither TTL expiration nor a clean checkout grants deletion authority.
    lease = new_lease(
        repository_id=f[4].load().repository_id,
        allocation_id=record(f).allocation_id,
        worktree_path=str(f[1]),
        owner=OWNER,
        holder=resolve_holder(str(os.getpid()), client_id="test"),
        ttl_seconds=60,
    )
    if expired:
        lease = replace(lease, expires_at="2000-01-01T00:00:00+00:00")
    f[4].write_writer_lease(lease)
    blockers = {b["code"] for b in cleanup(f, proofs)["plan"]["blockers"]}
    assert "WRITER_LEASE_PRESENT" in blockers
    assert f[4].load().writer_leases[record(f).allocation_id] == lease
    assert all((f[1] / relative / "opaque.bin").exists() for relative in PATHS)


def test_public_cleanup_cli_keeps_quarantine_fenced(failed_candidate, capsys):
    from worktree_controller.cli import main

    f = failed_candidate
    lifecycle.quarantine(
        f[0].repo,
        target=f[1],
        expected_record_version=1,
        owner=OWNER,
        expected_commit=git(f[1], "rev-parse", "HEAD"),
        reason="fixture quarantine",
    )
    assert (
        main(
            [
                "resource-clean",
                "--repo",
                str(f[0].repo),
                "--path",
                str(f[1]),
                "--expected-record-version",
                "2",
                "--owner",
                OWNER,
                "--typed",
                "--dry-run",
                "--json",
            ]
        )
        == 1
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["decision"]["code"] == "RESOURCE_CLEANUP_BLOCKED"
    assert record(f).lifecycle == "QUARANTINED"


def test_legacy_single_abort_receipt_remains_usable(failed_candidate):
    f = failed_candidate
    abort(f)
    current = record(f)
    event = {key: value for key, value in current.history[-1].items() if key != "abort_receipt"}
    f[4].replace_record(replace(current, history=(*current.history[:-1], event)))
    assert not cleanup(f, preserve(f))["plan"]["blockers"]
