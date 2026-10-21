from __future__ import annotations

import fcntl
import json
import os
import stat
from pathlib import Path

import pytest

from worktree_controller import activation_layout as layout
from worktree_controller.store import writer_lease_from_dict


@pytest.fixture
def migration(tmp_path, monkeypatch):
    # Unit fixtures do not share a process inventory with real Controller users.
    monkeypatch.setattr(layout, "refuse_running_entries", lambda names: None)
    repo = tmp_path / "repo"
    common = repo / ".git"
    old = common / "previous-controller/v1"
    old.mkdir(parents=True)
    for name in ("records", "writer-leases", "transactions", "completion-receipts"):
        (old / name).mkdir()
    (old / "repo.lock").touch()
    (old / "repo.json").write_bytes(b'{"historical": "unchanged"}\n')
    policy = repo / ".previous/worktree-policy.toml"
    policy.parent.mkdir()
    policy.write_bytes(b"schema_version = 6\n# exact permission policy\n")
    allocation = "24e42326-fb7f-46e4-9ef6-ddfcd688f9b5"
    (old / "records" / f"{allocation}.json").write_text('{"lifecycle":"ACTIVE"}')
    lease = {
        "schema_version": 1,
        "lease_id": "e44aa4c7-445a-41e5-a028-d865b297c652",
        "repository_id": "repository-one",
        "allocation_id": allocation,
        "worktree_path": str(tmp_path / "worktree"),
        "owner": "original-owner",
        "holder": {
            "provider": "executor-a",
            "pid": os.getpid(),
            "boot_id": "same-boot",
            "start_ticks": 123,
            "session_id": "same-session",
            "run_id": "same-run",
        },
        "acquired_at": "2026-09-13T10:00:00+08:00",
        "renewed_at": "2026-09-13T10:00:00+08:00",
        "expires_at": "2026-09-13T18:00:00+08:00",
        "record_version": 8,
    }
    lease_path = old / "writer-leases" / f"{allocation}.json"
    lease_path.write_text(json.dumps(lease))
    binary = tmp_path / "bin/previous-controller"
    binary.parent.mkdir()
    binary.write_bytes(b"#!/bin/sh\necho previous\n")
    binary.chmod(0o751)
    new_entry = binary.parent / "worktree-controller"
    arguments = dict(
        previous_namespace="previous-controller",
        previous_policy=".previous/worktree-policy.toml",
        previous_entries={binary: layout.digest(binary.read_bytes())},
        entries={new_entry: b"#!/bin/sh\necho current\n"},
        runtimes={"before": "one", "after": "two"},
    )
    receipt = tmp_path / "recovery/receipt.json"
    instance = layout.LayoutMigration.prepare(receipt, {repo: common}, **arguments)
    return instance, old, policy, lease_path, binary, new_entry, arguments


def test_exact_layout_migration_and_rollback_keep_authority_and_history(migration):
    change, old, policy, lease, binary, new_entry, _ = migration
    original_lease, original_policy, original_binary = (
        lease.read_bytes(),
        policy.read_bytes(),
        binary.read_bytes(),
    )
    inode = old.stat().st_ino
    history = (old / "repo.json").read_bytes()
    change.apply()
    row = change.document["repositories"][0]
    new = Path(row["new"])
    current = new / "writer-leases" / lease.name
    assert not old.exists() and not policy.exists()
    assert new.stat().st_ino == inode
    assert Path(row["new_policy"]).read_bytes() == original_policy
    assert (new / "repo.json").read_bytes() == history
    value = json.loads(current.read_bytes())
    parsed = writer_lease_from_dict(value)
    assert parsed.holder.client_id == "executor-a"
    value["schema_version"] = 1
    value["holder"]["provider"] = value["holder"].pop("client_id")
    assert value == json.loads(original_lease)
    assert binary.read_bytes() == layout.REFUSAL and new_entry.exists()
    assert stat.S_IMODE(change.receipt.stat().st_mode) == 0o600
    change.rollback()
    assert old.stat().st_ino == inode
    assert lease.read_bytes() == original_lease and policy.read_bytes() == original_policy
    assert binary.read_bytes() == original_binary and stat.S_IMODE(binary.stat().st_mode) == 0o751
    assert not new.exists() and not new_entry.exists()


def test_layout_rollback_preserves_entry_mode_under_restrictive_umask(migration):
    change, _, _, _, binary, new_entry, _ = migration
    previous = os.umask(0o077)
    try:
        change.apply()
        change.rollback()
    finally:
        os.umask(previous)
    assert stat.S_IMODE(binary.stat().st_mode) == 0o751
    assert not new_entry.exists()


@pytest.mark.parametrize("blocker", ["transaction", "publication", "policy-drift", "lease-schema"])
def test_new_blockers_fail_closed_before_fencing_or_moving(migration, blocker):
    change, old, policy, lease, binary, new_entry, _ = migration
    if blocker == "transaction":
        (old / "transactions/pending.json").write_text("{}")
    elif blocker == "publication":
        (old / "publication-lease.json").write_text("{}")
    elif blocker == "policy-drift":
        policy.write_text("changed permission policy")
    else:
        lease.write_text('{"schema_version":99}')
    before = binary.read_bytes()
    with pytest.raises((layout.LayoutError, ValueError, KeyError)):
        change.apply()
    assert old.exists() and policy.exists() and binary.read_bytes() == before
    assert not new_entry.exists()


def test_repository_lock_contention_cannot_move_state(migration):
    change, old, policy, _, binary, _, _ = migration
    with (old / "repo.lock").open("rb") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(BlockingIOError):
            change.apply()
    assert old.exists() and policy.exists() and binary.read_bytes() != layout.REFUSAL


def test_interrupted_activation_restores_the_already_moved_layout(migration, monkeypatch):
    change, old, policy, lease, binary, new_entry, _ = migration
    originals = lease.read_bytes(), policy.read_bytes(), binary.read_bytes()
    original = layout.replace_file

    def interrupt(path, raw, mode=0o600):
        if path == new_entry:
            raise KeyboardInterrupt
        return original(path, raw, mode)

    monkeypatch.setattr(layout, "replace_file", interrupt)
    with pytest.raises(KeyboardInterrupt):
        change.apply()
    monkeypatch.setattr(layout, "replace_file", original)
    restored = layout.LayoutMigration.load(
        change.receipt, layout.digest(change.receipt.read_bytes())
    )
    restored.rollback()
    assert (lease.read_bytes(), policy.read_bytes(), binary.read_bytes()) == originals
    assert old.exists() and not new_entry.exists()


def test_policy_move_interruption_is_recoverable_before_state_move(migration, monkeypatch):
    change, old, policy, _, binary, _, _ = migration
    original = os.rename

    def interrupt(source, target):
        if source == old:
            raise OSError("synthetic state move failure")
        return original(source, target)

    monkeypatch.setattr(os, "rename", interrupt)
    with pytest.raises(OSError):
        change.apply()
    monkeypatch.setattr(os, "rename", original)
    change.rollback()
    assert old.exists() and policy.exists() and binary.read_bytes() != layout.REFUSAL


def test_rollback_preserves_a_real_lease_renewal_instead_of_replaying_old_state(migration):
    change, old, _, lease, _, _, _ = migration
    change.apply()
    current = Path(change.document["repositories"][0]["new"]) / "writer-leases" / lease.name
    value = json.loads(current.read_bytes())
    value["renewed_at"] = "2026-09-13T11:00:00+08:00"
    value["record_version"] += 1
    current.write_text(json.dumps(value))
    change.rollback()
    restored = json.loads(lease.read_bytes())
    assert restored["renewed_at"] == value["renewed_at"]
    assert restored["record_version"] == value["record_version"]
    assert restored["holder"]["provider"] == value["holder"]["client_id"]
    assert old.exists()


def test_drift_and_unfinished_work_block_rollback_without_changing_authority(migration):
    change, old, _, _, binary, _, _ = migration
    change.apply()
    new = Path(change.document["repositories"][0]["new"])
    (new / "transactions/pending.json").write_text("{}")
    with pytest.raises(layout.LayoutError, match="unfinished transaction"):
        change.rollback()
    assert new.exists() and not old.exists() and binary.read_bytes() == layout.REFUSAL


def test_old_entries_retire_only_after_explicit_acceptance(migration):
    change, _, _, _, binary, new_entry, _ = migration
    change.apply()
    with pytest.raises(layout.LayoutError, match="verified activation"):
        change.finish()
    change.document["phase"] = "activated"
    change.save()
    change.finish()
    assert not binary.exists() and new_entry.exists()
    assert change.receipt.exists() and change.document["phase"] == "retired"


def test_changed_receipt_is_not_recovery_authority(migration):
    change, *_ = migration
    sha = layout.digest(change.receipt.read_bytes())
    change.receipt.write_text("{}")
    with pytest.raises(layout.LayoutError, match="digest changed"):
        layout.LayoutMigration.load(change.receipt, sha)


def test_rollback_never_overwrites_a_reappeared_previous_policy(migration):
    change, old, policy, _, binary, new_entry, _ = migration
    change.apply()
    policy.write_bytes(b"different live permissions")
    with pytest.raises(layout.LayoutError, match="overlapping or missing policies"):
        change.rollback()
    assert policy.read_bytes() == b"different live permissions"
    assert not old.exists() and binary.read_bytes() == layout.REFUSAL
    assert new_entry.read_bytes() != layout.REFUSAL


def test_rollback_fences_current_invocations_before_moving_their_namespace(migration, monkeypatch):
    change, old, policy, _, _, new_entry, _ = migration
    change.apply()

    def running(names):
        assert new_entry.name[:15] in names
        assert new_entry.read_bytes() == layout.REFUSAL
        raise layout.LayoutError("Controller invocation is still running")

    monkeypatch.setattr(layout, "refuse_running_entries", running)
    with pytest.raises(layout.LayoutError, match="still running"):
        change.rollback()
    assert not old.exists() and not policy.exists()
    monkeypatch.setattr(layout, "refuse_running_entries", lambda names: None)
    change.rollback()
    assert old.exists() and policy.exists() and not new_entry.exists()


@pytest.mark.parametrize("bad", ["", "/absolute", "../escape", "x/../y", "x/*", ".git/x"])
def test_previous_layout_requires_explicit_exact_paths(bad):
    with pytest.raises(layout.LayoutError):
        layout.relative(bad)


@pytest.mark.parametrize("phase", ["pass", "gate", "postcheck"])
def test_governed_activation_restores_layout_and_pointer_when_verification_fails(
    migration, monkeypatch, phase
):
    from types import SimpleNamespace

    from tests.test_rolling_upgrade import ROOT, load_activate

    change, old, policy, lease, binary, _, arguments = migration
    home = binary.parent.parent
    (home / ".local/bin").mkdir(parents=True)
    module = load_activate()
    install = home / "installation"
    before = install / "versions" / ("a" * 40) / "previous-package"
    after = install / "versions" / ("b" * 40) / "worktree-controller"
    before.mkdir(parents=True)
    after.mkdir(parents=True)
    active = install / "active"
    active.symlink_to(before)
    logs = home / "logs"
    logs.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(module, "ROOT", install)
    monkeypatch.setattr(module, "ACTIVE", active)
    monkeypatch.setattr(module, "ACTIVATION_LOCK", install / "activation.lock")
    monkeypatch.setattr(module, "LOG_DIR", logs)
    monkeypatch.setattr(module, "GOVERNED", (old.parent.parent.parent,))
    monkeypatch.setattr(module, "log", lambda text: None)
    monkeypatch.setattr(
        module, "previous_contract", lambda oid: {"entrypoint": "previous-controller"}
    )
    monkeypatch.setattr(module, "verify_runtime", lambda *a, **kw: "verified fixture runtime")
    monkeypatch.setattr(
        module, "run", lambda *a, **kw: SimpleNamespace(stdout=str(old.parent.parent))
    )
    calls = []

    def check_gate(*args):
        calls.append("gate")
        assert not old.exists() and not policy.exists()
        if phase == "gate":
            raise module.Abort("synthetic incompatible consumer")

    def check_post(*args):
        calls.append("postcheck")
        assert active.resolve() == after
        if phase == "postcheck":
            raise module.Abort("synthetic failed postcheck")

    monkeypatch.setattr(module, "preflight", check_gate)
    monkeypatch.setattr(module, "post_activation_check", check_post)
    options = SimpleNamespace(
        previous_entrypoint=[f"{binary}={arguments['previous_entries'][binary]}"],
        migrate_namespace_from="previous-controller",
        migrate_policy_from=".previous/worktree-policy.toml",
        consumer=[],
    )
    original = lease.read_bytes(), policy.read_bytes(), binary.read_bytes()
    if phase == "pass":
        module.migrate_layout(ROOT, after, "b" * 40, options, before)
        assert active.resolve() == after and not old.exists()
        assert calls == ["gate", "postcheck"]
    else:
        with pytest.raises(module.Abort):
            module.migrate_layout(ROOT, after, "b" * 40, options, before)
        assert active.resolve() == before
        assert (lease.read_bytes(), policy.read_bytes(), binary.read_bytes()) == original


@pytest.mark.parametrize(
    "payload", [{}, [], {"schema_version": True}, {"schema_version": 1, "holder": None}]
)
def test_malformed_lease_is_a_bounded_refusal(payload):
    with pytest.raises(layout.LayoutError):
        layout.lease_bytes(json.dumps(payload).encode())
