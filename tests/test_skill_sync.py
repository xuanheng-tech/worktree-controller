"""Skill delivery uses committed ancestry and leaves the active runtime untouched."""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.test_rolling_upgrade import load_activate
from tests.test_shadow import git


def identity(path: Path, *, symlink: bool = False) -> tuple[int, ...]:
    info = path.lstat() if symlink else path.stat()
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


@pytest.fixture
def installation(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    repo = tmp_path / "repo"
    git(tmp_path, "init", "-b", "main", str(repo))
    git(repo, "config", "user.name", "Skill Fixture")
    git(repo, "config", "user.email", "fixture@example.invalid")
    source = repo / "skills/worktree-lifecycle"
    (source / "agents").mkdir(parents=True)
    old, new, companion = (
        b"committed old Skill\n",
        b"committed active Skill\n",
        b"display: fixture\n",
    )
    (source / "SKILL.md").write_bytes(old)
    (source / "agents/openai.yaml").write_bytes(companion)
    git(repo, "add", "skills")
    git(repo, "commit", "-m", "old Skill")
    ancestor = git(repo, "rev-parse", "HEAD")
    (source / "SKILL.md").write_bytes(new)
    git(repo, "add", "skills")
    git(repo, "commit", "-m", "active Skill")
    oid = git(repo, "rev-parse", "HEAD")

    module = load_activate()
    home = tmp_path / "home"
    root = home / ".local/share/worktree-controller"
    runtime = root / "versions" / oid / "worktree-controller"
    runtime.mkdir(parents=True)
    active = root / "active"
    active.symlink_to(runtime)
    target = home / ".agents/skills/worktree-lifecycle"
    (target / "agents").mkdir(parents=True)
    (target / "SKILL.md").write_bytes(old)
    (target / "SKILL.md").chmod(0o640)
    (target / "agents/openai.yaml").write_bytes(companion)
    (home / ".claude").mkdir()
    bridge = home / ".claude/skills"
    bridge.symlink_to("../.agents/skills")
    for name, value in {
        "CONTROLLER_REPO": repo,
        "GIT_SOURCE": f"file://{repo}",
        "ROOT": root,
        "VERSIONS": root / "versions",
        "ACTIVE": active,
        "ACTIVATION_LOCK": root / "activation.lock",
        "LOG_DIR": home / ".local/state/rollout",
        "SKILL_TARGET": target,
    }.items():
        monkeypatch.setattr(module, name, value)
    verified = []
    monkeypatch.setattr(
        module, "verify_runtime", lambda path, commit: verified.append((path, commit))
    )
    return SimpleNamespace(
        module=module,
        repo=repo,
        source=source,
        target=target,
        old=old,
        new=new,
        companion=companion,
        ancestor=ancestor,
        oid=oid,
        active=active,
        runtime=runtime,
        bridge=bridge,
        verified=verified,
    )


def test_dry_run_proves_ancestry_and_changes_no_payload_or_receipt(installation):
    item = installation
    pointer = identity(item.active, symlink=True)
    reply = item.module.sync_skill(item.oid, dry_run=True)
    assert reply["result"] == "SKILL_SYNC_READY"
    assert reply["installed_source_commit"] == item.ancestor
    assert item.verified == [(item.runtime, item.oid)]
    assert (item.target / "SKILL.md").read_bytes() == item.old
    assert identity(item.active, symlink=True) == pointer
    assert not item.module.LOG_DIR.exists()


def test_committed_source_repairs_only_skill_and_preserves_claude_bridge(installation):
    item = installation
    (item.source / "SKILL.md").write_bytes(b"uncommitted source must never install\n")
    pointer = identity(item.active, symlink=True)
    bridge = identity(item.bridge, symlink=True)
    companion = identity(item.target / "agents/openai.yaml")
    reply = item.module.sync_skill(item.oid)
    assert reply["result"] == "SKILL_SYNCED"
    assert (item.target / "SKILL.md").read_bytes() == item.new
    assert (item.target / "SKILL.md").stat().st_mode & 0o777 == 0o640
    assert identity(item.target / "agents/openai.yaml") == companion
    assert identity(item.active, symlink=True) == pointer and item.active.resolve() == item.runtime
    assert (
        identity(item.bridge, symlink=True) == bridge
        and os.readlink(item.bridge) == "../.agents/skills"
    )
    receipt = json.loads(Path(reply["receipt"]).read_text())
    assert receipt["source_commit"] == item.oid
    assert receipt["installed_source_commit"] == item.ancestor
    assert "committed active Skill" not in Path(reply["receipt"]).read_text()


def test_no_change_apply_writes_proof_without_replacing_current_skill(installation, monkeypatch):
    item = installation
    target = item.target / "SKILL.md"
    target.write_bytes(item.new)
    before = identity(target)
    replace = item.module.replace_file

    def receipt_only(path, *args):
        assert path != target
        return replace(path, *args)

    monkeypatch.setattr(item.module, "replace_file", receipt_only)
    assert item.module.sync_skill(item.oid, dry_run=True)["result"] == "SKILL_ALREADY_CURRENT"
    assert not item.module.LOG_DIR.exists()
    reply = item.module.sync_skill(item.oid)
    assert reply["result"] == "SKILL_ALREADY_CURRENT"
    assert Path(reply["receipt"]).is_file()
    assert identity(target) == before


def rolled_back(item):
    runtime = item.module.runtime_path(item.ancestor)
    runtime.mkdir(parents=True)
    item.active.unlink()
    item.active.symlink_to(runtime)
    (item.target / "SKILL.md").write_bytes(item.new)
    return runtime


def test_runtime_rollback_still_requires_explicit_skill_origin(installation):
    item = installation
    rolled_back(item)
    with pytest.raises(item.module.Abort, match="committed active ancestor"):
        item.module.sync_skill(item.ancestor)
    assert (item.target / "SKILL.md").read_bytes() == item.new
    assert not item.module.LOG_DIR.exists()


def test_exact_installed_descendant_supports_dry_run_apply_and_repeat(installation):
    item = installation
    runtime = rolled_back(item)
    target = item.target / "SKILL.md"
    before = identity(target)
    pointer = identity(item.active, symlink=True)
    reply = item.module.sync_skill(item.ancestor, rollback_from=item.oid, dry_run=True)
    assert reply["result"] == "SKILL_SYNC_READY"
    assert reply["installed_source_commit"] == item.oid
    assert reply["rollback_from_commit"] == item.oid
    assert item.verified == [(runtime, item.ancestor), (item.runtime, item.oid)]
    assert identity(target) == before and not item.module.LOG_DIR.exists()
    reply = item.module.sync_skill(item.ancestor, rollback_from=item.oid)
    assert reply["result"] == "SKILL_SYNCED"
    assert target.read_bytes() == item.old
    assert target.stat().st_mode & 0o777 == 0o640
    assert identity(item.active, symlink=True) == pointer
    current = identity(target)
    repeat = item.module.sync_skill(item.ancestor, rollback_from=item.oid)
    assert repeat["result"] == "SKILL_ALREADY_CURRENT"
    assert identity(target) == current
    assert Path(reply["receipt"]).is_file() and Path(repeat["receipt"]).is_file()


@pytest.mark.parametrize("previous", ["main", "short", "same", "unknown", "unrelated"])
def test_skill_rollback_refuses_invalid_or_unrelated_source(installation, previous):
    item = installation
    rolled_back(item)
    if previous == "unrelated":
        git(item.repo, "checkout", "--orphan", "unrelated")
        git(item.repo, "commit", "-m", "unrelated root")
        value = git(item.repo, "rev-parse", "HEAD")
    else:
        value = {"main": "main", "short": item.oid[:12], "same": item.ancestor,
                 "unknown": "0" * 40}[previous]
    with pytest.raises(item.module.Abort):
        item.module.sync_skill(item.ancestor, rollback_from=value)
    assert (item.target / "SKILL.md").read_bytes() == item.new
    assert not item.module.LOG_DIR.exists()


@pytest.mark.parametrize("drift", ["skill", "companion", "runtime"])
def test_skill_rollback_refuses_payload_or_runtime_drift(installation, monkeypatch, drift):
    item = installation
    rolled_back(item)
    if drift == "runtime":
        def refuse(path, oid):
            if oid == item.oid:
                raise item.module.Abort("fixture previous runtime is missing or changed")
        monkeypatch.setattr(item.module, "verify_runtime", refuse)
    else:
        name = "SKILL.md" if drift == "skill" else "agents/openai.yaml"
        (item.target / name).write_bytes(b"unknown installed content\n")
    before = {p: p.read_bytes() for p in item.target.rglob("*") if p.is_file()}
    with pytest.raises(item.module.Abort):
        item.module.sync_skill(item.ancestor, rollback_from=item.oid)
    assert all(path.read_bytes() == raw for path, raw in before.items())
    assert not item.module.LOG_DIR.exists()


def test_skill_rollback_option_is_rejected_for_other_actions(installation, monkeypatch):
    item = installation
    monkeypatch.setattr("sys.argv", ["controller-activate", "status",
                                    "--skill-rollback-from", item.oid])
    with pytest.raises(SystemExit) as failure:
        item.module.main()
    assert failure.value.code == 2
    assert not item.module.LOG_DIR.exists()


@pytest.mark.parametrize("version", ["main", "short", "ancestor", "0" * 40])
def test_sync_requires_exact_current_full_oid(installation, version):
    item = installation
    version = (
        item.oid[:12] if version == "short" else item.ancestor if version == "ancestor" else version
    )
    with pytest.raises(item.module.Abort):
        item.module.sync_skill(version)
    assert (item.target / "SKILL.md").read_bytes() == item.old
    assert not item.module.LOG_DIR.exists()


def test_runtime_verification_failure_prevents_skill_write(installation, monkeypatch):
    item = installation

    def refuse(*args):
        raise item.module.Abort("fixture runtime bytes differ")

    monkeypatch.setattr(item.module, "verify_runtime", refuse)
    with pytest.raises(item.module.Abort, match="runtime bytes differ"):
        item.module.sync_skill(item.oid)
    assert (item.target / "SKILL.md").read_bytes() == item.old


@pytest.mark.parametrize("origin", ["uncommitted", "unrelated"])
def test_unknown_installed_skill_never_overwrites_user_content(installation, origin):
    item = installation
    unknown = b"unknown installed instructions\n"
    if origin == "unrelated":
        git(item.repo, "checkout", "-b", "unrelated", item.ancestor)
        (item.source / "SKILL.md").write_bytes(unknown)
        git(item.repo, "add", "skills")
        git(item.repo, "commit", "-m", "unrelated Skill")
        git(item.repo, "checkout", "main")
    (item.target / "SKILL.md").write_bytes(unknown)
    with pytest.raises(item.module.Abort, match="committed active ancestor"):
        item.module.sync_skill(item.oid)
    assert (item.target / "SKILL.md").read_bytes() == unknown
    assert item.active.resolve() == item.runtime


@pytest.mark.parametrize(
    "drift", ["companion", "missing", "unknown", "file-link", "dir-link", "root-link"]
)
def test_companion_and_path_drift_fail_before_reading_unknown_contents(
    installation, monkeypatch, drift
):
    item = installation
    companion = item.target / "agents/openai.yaml"
    unknown = item.target.parent / "outside"
    unknown.write_bytes(b"fixture outside contents must not be read\n")
    if drift == "companion":
        companion.write_bytes(b"changed companion\n")
    elif drift == "missing":
        companion.unlink()
    elif drift == "unknown":
        (item.target / "unexpected").write_bytes(b"unknown\n")
    elif drift == "file-link":
        companion.unlink()
        companion.symlink_to(unknown)
    elif drift == "dir-link":
        (item.target / "agents").rename(item.target.parent / "old-agents")
        (item.target / "agents").symlink_to(item.target.parent / "old-agents")
    else:
        item.target.rename(item.target.parent / "old-target")
        item.target.symlink_to(item.target.parent / "old-target")
    read_bytes = Path.read_bytes

    def read_known(path):
        assert path not in {unknown, item.target / "unexpected"}
        if drift != "companion":
            assert not path.is_relative_to(item.target)
        return read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", read_known)
    with pytest.raises((item.module.Abort, item.module.LayoutError)):
        item.module.sync_skill(item.oid)
    assert item.active.resolve() == item.runtime
    assert not item.module.LOG_DIR.exists()


@pytest.mark.parametrize("drift", ["active", "skill", "companion"])
def test_lock_rechecks_preflight_identity_before_writing(installation, monkeypatch, drift):
    item = installation
    target = item.target / "SKILL.md"

    @contextmanager
    def changed_lock():
        if drift == "active":
            other = item.module.VERSIONS / ("f" * 40) / "worktree-controller"
            other.mkdir(parents=True)
            item.active.unlink()
            item.active.symlink_to(other)
        elif drift == "skill":
            target.write_bytes(b"new user work\n")
        else:
            (item.target / "agents/openai.yaml").write_bytes(b"new companion\n")
        yield

    monkeypatch.setattr(item.module, "activation_lock", changed_lock)
    with pytest.raises(item.module.Abort):
        item.module.sync_skill(item.oid)
    assert target.read_bytes() == (b"new user work\n" if drift == "skill" else item.old)
    assert not item.module.LOG_DIR.exists()


@pytest.mark.parametrize(
    "phase", ["before", "after", "receipt", "receipt-after", "postcheck", "interrupt"]
)
def test_atomic_failure_restores_old_payload_mode_and_pointer(installation, monkeypatch, phase):
    item = installation
    target = item.target / "SKILL.md"
    pointer = identity(item.active, symlink=True)
    replace, scan = item.module.replace_file, item.module.installed_skill
    written, scans = 0, 0

    def write(path, *args):
        nonlocal written
        if path == target:
            written += 1
            if written == 1 and phase == "before":
                raise OSError("fixture atomic write failed")
            replace(path, *args)
            if written == 1 and phase == "after":
                raise OSError("fixture post-rename fsync failed")
            if written == 1 and phase == "interrupt":
                raise KeyboardInterrupt
        elif phase == "receipt":
            raise OSError("fixture receipt write failed")
        else:
            replace(path, *args)
            if phase == "receipt-after":
                raise OSError("fixture receipt fsync failed")

    def inspect():
        nonlocal scans
        scans += 1
        if scans == 3 and phase == "postcheck":
            raise item.module.Abort("fixture postcheck failed")
        return scan()

    monkeypatch.setattr(item.module, "replace_file", write)
    monkeypatch.setattr(item.module, "installed_skill", inspect)
    with pytest.raises((OSError, item.module.Abort, KeyboardInterrupt)):
        item.module.sync_skill(item.oid)
    assert target.read_bytes() == item.old
    assert target.stat().st_mode & 0o777 == 0o640
    assert identity(item.active, symlink=True) == pointer
    assert not list(item.module.LOG_DIR.glob("*.json"))
    assert not list(item.target.glob(".*pending"))


def test_recovery_does_not_follow_a_parent_symlink_created_after_write(installation, monkeypatch):
    item = installation
    replace = item.module.replace_file
    moved = item.repo.parent / "moved-skills"
    pointer = identity(item.active, symlink=True)

    def moved_parent(path, *args):
        replace(path, *args)
        item.target.parent.rename(moved)
        item.target.parent.symlink_to(moved)

    monkeypatch.setattr(item.module, "replace_file", moved_parent)
    with pytest.raises(item.module.LayoutError, match="owned directory"):
        item.module.sync_skill(item.oid)
    assert (moved / "worktree-lifecycle/SKILL.md").read_bytes() == item.new
    assert identity(item.active, symlink=True) == pointer
    assert not item.module.LOG_DIR.exists()


def test_recovery_preserves_unknown_concurrent_skill_changes(installation, monkeypatch):
    item = installation
    replace = item.module.replace_file
    target = item.target / "SKILL.md"

    def changed_payload(path, *args):
        replace(path, *args)
        target.write_bytes(b"concurrent unknown instructions\n")

    monkeypatch.setattr(item.module, "replace_file", changed_payload)
    with pytest.raises(item.module.Abort, match="blocks automatic restoration"):
        item.module.sync_skill(item.oid)
    assert target.read_bytes() == b"concurrent unknown instructions\n"
    assert item.active.resolve() == item.runtime


def test_committed_symlink_blob_is_not_a_trusted_ancestor(installation):
    item = installation
    source = item.source / "SKILL.md"
    source.unlink()
    source.symlink_to("symlink-payload")
    git(item.repo, "add", "skills")
    git(item.repo, "commit", "-m", "symlink Skill is not a trusted payload")
    source.unlink()
    source.write_bytes(item.new)
    git(item.repo, "add", "skills")
    git(item.repo, "commit", "-m", "restore regular Skill")
    oid = git(item.repo, "rev-parse", "HEAD")
    runtime = item.module.VERSIONS / oid / "worktree-controller"
    runtime.mkdir(parents=True)
    item.active.unlink()
    item.active.symlink_to(runtime)
    (item.target / "SKILL.md").write_bytes(b"symlink-payload")
    with pytest.raises(item.module.Abort, match="committed active ancestor"):
        item.module.sync_skill(oid)
    assert (item.target / "SKILL.md").read_bytes() == b"symlink-payload"


@pytest.mark.parametrize("action", ["stage", "activate", "rollback"])
def test_non_skill_actions_reject_dry_run_before_any_runtime_operation(
    installation, monkeypatch, action
):
    item = installation
    monkeypatch.setattr(
        "sys.argv", ["controller-activate", action, "--version", item.oid, "--dry-run"]
    )
    with pytest.raises(SystemExit) as failure:
        item.module.main()
    assert failure.value.code == 2
    assert not item.verified and not item.module.LOG_DIR.exists()
