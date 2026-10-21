"""Published hotfix environment classification preserves frozen source evidence."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from tests.test_controlled import (
    _commit_reproducible_project,
    _create_reproducible_venv,
    acquire_one,
    adopt_canonical,
    handoff_one,
)
from tests.test_retention_admission import POLICY_V5
from tests.test_shadow import GitRepository, git
from worktree_controller import lifecycle
from worktree_controller.models import ControllerError, PublicationLease, WriterHolder, WriterLease
from worktree_controller.store import MetadataStore, now_iso, record_to_dict


@pytest.fixture
def published(request, monkeypatch):
    monkeypatch.setattr(lifecycle, "_validate_runtime_clear", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        lifecycle,
        "_typed_runtime_evidence",
        lambda *_args: {
            "managed_references": [],
            "observed_references": [],
            "managed_visibility_uncertainties": [],
            "scan_uncertainties": [],
        },
    )
    with GitRepository(linked=0, policy_text=POLICY_V5) as repo:
        _commit_reproducible_project(repo, extra_ignore="unknown")
        adopt_canonical(repo)
        remote = repo.root / "published.git"
        git(repo.root, "init", "--bare", str(remote))
        git(repo.repo, "remote", "add", "origin", str(remote))
        git(repo.repo, "push", "origin", "main")
        path, acquired = acquire_one(
            repo, task="published-hotfix", role="hotfix", scope_paths=("result.txt",)
        )
        (path / "result.txt").write_text("synthetic hotfix result\n")
        git(path, "add", "result.txt")
        git(path, "commit", "-m", "synthetic hotfix")
        head = git(path, "rev-parse", "HEAD")
        if getattr(request, "param", True):
            git(path, "push", "origin", "HEAD:main")
        git(repo.repo, "fetch", "origin", "main")
        venv = _create_reproducible_venv(path)
        handoff_one(repo, path)
        store = MetadataStore(repo.repo / ".git")
        allocation = acquired["decision"]["allocation_id"]
        yield SimpleNamespace(
            repo=repo,
            path=path,
            venv=venv,
            store=store,
            allocation=allocation,
            head=head,
            args=dict(
                target=path,
                expected_record_version=2,
                owner="module-owner",
                resource_path=".venv",
                kind="python-venv",
                reproducible=True,
                creator="handoff-owner-attestation:module-owner",
                teardown_policy="on-release",
            ),
        )


def _files(root):
    return {
        str(p.relative_to(root)): p.read_bytes()
        for p in root.rglob("*")
        if p.is_file() and not p.is_symlink()
    }


def _record(fixture, **changes):
    with fixture.store.lock(exclusive=True):
        current = fixture.store._load_unlocked().records[fixture.allocation]
        fixture.store.replace_record(replace(current, **changes))


def _reject(fixture, code, **arguments):
    before = _files(fixture.store.root)
    source = _files(fixture.venv)
    refs = git(fixture.repo.repo, "show-ref")
    head = git(fixture.path, "rev-parse", "HEAD")
    index = git(fixture.path, "diff", "--cached", "--raw")
    with pytest.raises(ControllerError) as blocked:
        lifecycle.register_disposable(fixture.repo.repo, **{**fixture.args, **arguments})
    assert blocked.value.code == code
    assert _files(fixture.store.root) == before
    assert _files(fixture.venv) == source
    assert git(fixture.repo.repo, "show-ref") == refs
    assert git(fixture.path, "rev-parse", "HEAD") == head
    assert git(fixture.path, "diff", "--cached", "--raw") == index


def test_published_hotfix_classification_is_metadata_only_and_keeps_existing_release(published):
    f = published
    before = record_to_dict(f.store.load().records[f.allocation])
    source = _files(f.venv)
    refs = git(f.repo.repo, "show-ref")
    result = lifecycle.register_disposable(f.repo.repo, **f.args)
    assert result["decision"]["code"] == "DISPOSABLE_REGISTERED"
    assert result["decision"]["record_version"] == 3
    after = record_to_dict(f.store.load().records[f.allocation])
    for key in before.keys() - {
        "record_version",
        "last_reviewed_at",
        "history",
        "disposable_resources",
    }:
        assert after[key] == before[key]
    assert after["history"][:-1] == before["history"]
    event = after["history"][-1]
    assert event["event"] == "DISPOSABLE_REGISTERED"
    proof = event["published_handoff_resource_registration"]
    assert proof["contract"] == "published-handoff-resource-registration/v1"
    assert proof["allocation_id"] == f.allocation and proof["expected_record_version"] == 2
    assert proof["owner"] == "module-owner"
    assert proof["creator"] == f.args["creator"]
    assert proof["frozen_identity"]["head"] == f.head
    assert proof["publication"]["oid"] == f.head
    assert proof["environment"]["environment_path"] == ".venv"
    assert len(proof["environment"]["environment_identity_sha256"]) == 64
    assert set(after["disposable_resources"][0]) == {
        "resource_id",
        "path",
        "kind",
        "reproducible",
        "creator",
        "teardown_policy",
        "state",
    }
    assert _files(f.venv) == source and git(f.repo.repo, "show-ref") == refs
    preview = lifecycle.release(
        f.repo.repo,
        target=f.path,
        expected_record_version=3,
        owner_release="module-owner",
        teardown_reproducible_venv=True,
        dry_run=True,
    )
    assert preview["decision"]["code"] == "RELEASE_PLAN_READY"
    assert _files(f.venv) == source
    released = lifecycle.release(
        f.repo.repo,
        target=f.path,
        expected_record_version=3,
        owner_release="module-owner",
        teardown_reproducible_venv=True,
    )
    assert released["decision"]["code"] == "RELEASED"
    assert not f.path.exists()
    assert git(f.repo.repo, "rev-parse", "feature/published-hotfix") == f.head
    record = f.store.load().records[f.allocation]
    assert record.history[: len(after["history"])] == tuple(after["history"])


@pytest.mark.parametrize(
    "changes,code",
    [
        ({"owner": "foreign-owner"}, "OWNER_MISMATCH"),
        ({"expected_record_version": 1}, "CAS_MISMATCH"),
        ({"creator": "uv sync"}, "INVALID_RESOURCE"),
        ({"creator": "handoff-owner-attestation:foreign-owner"}, "INVALID_RESOURCE"),
        ({"reproducible": False}, "INVALID_RESOURCE"),
        ({"teardown_policy": "manual"}, "INVALID_RESOURCE"),
    ],
)
def test_published_hotfix_requires_exact_owner_cas_and_truthful_resource_attestation(
    published, changes, code
):
    _reject(published, code, **changes)


@pytest.mark.parametrize(
    "field,code",
    [
        ("task_commit", "IDENTITY_MISMATCH"),
        ("handoff_commit", "MISSING_EVIDENCE"),
        ("finalizer_commit", "IDENTITY_MISMATCH"),
        ("finalizer_record_id", "MISSING_EVIDENCE"),
        ("base_commit", "TASK_LINEAGE_UNREACHABLE"),
    ],
)
def test_published_hotfix_requires_complete_frozen_identity(published, field, code):
    _record(published, **{field: None})
    _reject(published, code)


@pytest.mark.parametrize("field", ["task_commit", "finalizer_commit"])
def test_published_hotfix_refuses_different_frozen_commit(published, field):
    base = published.store.load().records[published.allocation].base_commit
    _record(published, **{field: base})
    _reject(published, "IDENTITY_MISMATCH")


@pytest.mark.parametrize(
    "field",
    [
        "quarantine_reason",
        "quarantine_expected_commit",
        "quarantine_observed_commit",
        "recovery_ref",
        "recovery_allocation_id",
    ],
)
def test_published_hotfix_does_not_consume_recovery_lineage(published, field):
    value = (
        str(uuid4())
        if field == "recovery_allocation_id"
        else "refs/heads/feature/published-hotfix"
        if field == "recovery_ref"
        else published.head
    )
    _record(published, **{field: value})
    _reject(published, "RECOVERY_STATE")


@pytest.mark.parametrize("commit", [False, True])
def test_published_hotfix_refuses_dirty_or_head_drift(published, commit):
    (published.path / "result.txt").write_text("changed hotfix result\n")
    if commit:
        git(published.path, "add", "result.txt")
        git(published.path, "commit", "-m", "drift")
    _reject(published, "HEAD_MISMATCH" if commit else "DIRTY_WORKTREE")


@pytest.mark.parametrize("published", [False], indirect=True)
def test_unpublished_hotfix_cannot_classify_frozen_environment(published):
    _reject(published, "DELIVERABLE_NOT_PUBLISHED")


@pytest.mark.parametrize(
    "change,code",
    [
        ("stale-tracking", "PUBLICATION_TRACKING_STALE"),
        ("missing-remote", "PUBLICATION_UNVERIFIED"),
        ("equivalent-tree", "DELIVERABLE_NOT_PUBLISHED"),
    ],
)
def test_published_hotfix_requires_live_exact_publication_ancestry(published, change, code):
    f = published
    if change == "stale-tracking":
        git(
            f.repo.repo,
            "update-ref",
            "refs/remotes/origin/main",
            f.store.load().records[f.allocation].base_commit,
        )
    elif change == "missing-remote":
        git(f.repo.repo, "remote", "remove", "origin")
    else:
        equivalent = git(f.repo.repo, "commit-tree", f"{f.head}^{{tree}}", "-m", "equivalent root")
        git(f.repo.repo, "push", "origin", f"{equivalent}:refs/heads/equivalent")
        git(f.repo.root / "published.git", "update-ref", "refs/heads/main", equivalent)
        git(f.repo.repo, "update-ref", "refs/remotes/origin/main", equivalent)
    _reject(f, code)


def test_published_hotfix_requires_retained_branch_identity(published, monkeypatch):
    original = lifecycle.resolve_commit
    branch = "refs/heads/feature/published-hotfix"
    base = published.store.load().records[published.allocation].base_commit
    monkeypatch.setattr(
        lifecycle,
        "resolve_commit",
        lambda repo, ref: base if ref == branch else original(repo, ref),
    )
    _reject(published, "MISSING_RETAINED_REF")


@pytest.mark.parametrize("lease", ["writer", "publication"])
def test_published_hotfix_refuses_registered_leases(published, lease):
    f = published
    snapshot = f.store.load()
    timestamp = now_iso()
    expiry = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    with f.store.lock(exclusive=True):
        if lease == "writer":
            f.store.write_writer_lease(
                WriterLease(
                    schema_version=2,
                    lease_id=str(uuid4()),
                    repository_id=snapshot.repository_id,
                    allocation_id=f.allocation,
                    worktree_path=str(f.path),
                    owner="module-owner",
                    holder=WriterHolder(
                        client_id="test",
                        pid=12345,
                        boot_id=str(uuid4()),
                        start_ticks=1,
                        session_id=None,
                        run_id="synthetic",
                    ),
                    acquired_at=timestamp,
                    renewed_at=timestamp,
                    expires_at=expiry,
                    record_version=2,
                )
            )
        else:
            f.store.write_publication_lease(
                PublicationLease(
                    schema_version=2,
                    lease_id=str(uuid4()),
                    repository_id=snapshot.repository_id,
                    allocation_id=f.allocation,
                    holder="module-owner",
                    run_id="synthetic",
                    remote="origin",
                    branch="main",
                    target_ref="refs/heads/main",
                    candidate_oid=f.head,
                    expected_main_oid=f.head,
                    validation_evidence_id="synthetic-validation",
                    acquired_at=timestamp,
                    expires_at=expiry,
                )
            )
    _reject(f, "WRITER_LEASE_PRESENT" if lease == "writer" else "PUBLICATION_LEASE_HELD")


def test_published_hotfix_refuses_external_binding(published):
    _record(published, external_bindings=("synthetic-runtime-binding",))
    _reject(published, "EXTERNAL_BINDING")


@pytest.mark.parametrize(
    "field,value,code",
    [
        ("managed_references", [{"kind": "execution"}], "MANAGED_REFERENCE"),
        ("observed_references", [{"kind": "cwd", "pid": 12345}], "RUNTIME_REFERENCE"),
        ("managed_visibility_uncertainties", ["managed-unreadable"], "RUNTIME_SCAN_UNCERTAIN"),
        ("scan_uncertainties", ["identity-changed"], "RUNTIME_SCAN_UNCERTAIN"),
    ],
)
def test_published_hotfix_refuses_runtime_use_or_uncertainty(
    published, monkeypatch, field, value, code
):
    evidence = {
        "managed_references": [],
        "observed_references": [],
        "managed_visibility_uncertainties": [],
        "scan_uncertainties": [],
    }
    evidence[field] = value
    monkeypatch.setattr(lifecycle, "_typed_runtime_evidence", lambda *_args: evidence)
    _reject(published, code)


@pytest.mark.parametrize("state", ["open", "abandoned", "unrelated-handoff", "invalid-terminal"])
def test_published_hotfix_keeps_exact_integration_terminal_authority(published, state):
    f = published
    queued = lifecycle.enqueue_integration(
        f.repo.repo,
        task_key="hotfix-old-integration",
        authority_key="hotfix-old-integration",
        module="integration",
        owner="integration-owner",
        source_allocations=(f.allocation,),
    )
    intent_id = queued["decision"]["intent_id"]
    if state != "open":
        lifecycle.abandon_integration_intent(
            f.repo.repo,
            intent_id=intent_id,
            owner="integration-owner",
            reason="synthetic formal abandonment",
        )
    if state in {"unrelated-handoff", "invalid-terminal"}:
        with f.store.lock(exclusive=True):
            intent = f.store._load_unlocked().integration_intents[intent_id]
            changes = (
                dict(
                    source_handoffs=(
                        git(f.repo.repo, "commit-tree", "HEAD^{tree}", "-m", "unrelated source"),
                    )
                )
                if state == "unrelated-handoff"
                else dict(history=intent.history[:-1])
            )
            f.store.replace_integration_intent(replace(intent, **changes))
    before = (f.store.integration_intents_dir / f"{intent_id}.json").read_bytes()
    if state == "abandoned":
        assert (
            lifecycle.register_disposable(f.repo.repo, **f.args)["decision"]["code"]
            == "DISPOSABLE_REGISTERED"
        )
    else:
        _reject(f, "INTEGRATION_OBLIGATION")
    assert (f.store.integration_intents_dir / f"{intent_id}.json").read_bytes() == before


def test_published_hotfix_refuses_other_live_source_allocation(published):
    acquire_one(
        published.repo,
        task="other-candidate",
        authority="other-candidate",
        role="integration",
        source_allocations=(published.allocation,),
    )
    _reject(published, "INTEGRATION_OBLIGATION")


@pytest.mark.parametrize("unknown", [False, True])
def test_published_hotfix_refuses_pending_or_unknown_transaction_without_rewriting_it(
    published, unknown
):
    f = published
    transaction = str(uuid4())
    value = (
        {"unknown": True}
        if unknown
        else dict(
            schema_version=1,
            transaction_id=transaction,
            repository_id=f.store.load().repository_id,
            allocation_id=f.allocation,
            target_path=str(f.path),
            command="resource-clean",
            phase="PREPARED",
            actions=[],
        )
    )
    with f.store.lock(exclusive=True):
        f.store.write_transaction(transaction, value)
    _reject(f, "INTERRUPTED_TRANSACTION")


@pytest.mark.parametrize(
    "change,code",
    [
        ("symlink", "RESOURCE_IDENTITY_MISMATCH"),
        ("mount", "RESOURCE_IDENTITY_MISMATCH"),
        ("unignored", "RESOURCE_NOT_IGNORED"),
        ("tracked", "TRACKED_RESOURCE"),
        ("missing-cfg", "RESOURCE_IDENTITY_MISMATCH"),
        ("invalid-cfg", "RESOURCE_IDENTITY_MISMATCH"),
        ("invalid-cache", "RESOURCE_IDENTITY_MISMATCH"),
        ("missing-python", "RESOURCE_IDENTITY_MISMATCH"),
        ("unknown-content", "IGNORED_CONTENT"),
    ],
)
def test_published_hotfix_requires_existing_reproducible_task_local_environment(
    published, monkeypatch, change, code
):
    f = published
    if change == "symlink":
        original = f.venv.rename(f.path / "original-env")
        f.venv.symlink_to(original, target_is_directory=True)
    elif change == "mount":
        is_mount = Path.is_mount
        monkeypatch.setattr(Path, "is_mount", lambda path: path == f.venv or is_mount(path))
    elif change == "unignored":
        (f.path / ".gitignore").write_text("")
    elif change == "tracked":
        git(f.path, "add", "-f", ".venv/payload.bin")
    elif change == "missing-cfg":
        (f.venv / "pyvenv.cfg").unlink()
    elif change == "invalid-cfg":
        (f.venv / "pyvenv.cfg").write_text("home = /synthetic\n")
    elif change == "invalid-cache":
        (f.venv / "CACHEDIR.TAG").write_text("unknown environment\n")
    elif change == "missing-python":
        (f.venv / "bin/python").unlink()
    else:
        (f.path / "unknown").mkdir()
        (f.path / "unknown/persistent.txt").write_text("must remain untouched\n")
    _reject(f, code)
