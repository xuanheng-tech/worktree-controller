"""Exported-history rollout uses real Git identities and immutable producer evidence."""

from __future__ import annotations

import fcntl
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.test_rolling_upgrade import ROOT, load_activate
from tests.test_shadow import git


@pytest.fixture
def handover(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    module = load_activate()
    baseline_source = Path(os.environ.get("WC_HISTORY_BASELINE_SOURCE", str(ROOT)))
    source = tmp_path / "public"
    private = tmp_path / "previous"
    for repo, message in ((source, "public export"), (private, "previous private history")):
        git(tmp_path, "init", "-b", "main", str(repo))
        git(repo, "config", "user.name", "History Fixture")
        git(repo, "config", "user.email", "fixture@example.invalid")
        for name in module.EXPORT_PATHS:
            target = repo / name
            target.parent.mkdir(parents=True, exist_ok=True)
            if (baseline_source / name).is_dir():
                shutil.copytree(baseline_source / name, target, ignore=shutil.ignore_patterns("__pycache__"))
            else:
                shutil.copyfile(baseline_source / name, target)
        git(repo, "add", ".")
        git(repo, "commit", "-m", message)
    previous = git(private, "rev-parse", "HEAD")
    baseline = git(source, "rev-parse", "HEAD")
    version = json.loads((source / "tool_cli_contract.json").read_text())["tool_version"]
    git(source, "tag", f"v{version}")
    for name in module.EXPORT_PATHS:
        if (ROOT / name).is_dir():
            shutil.copytree(ROOT / name, source / name, dirs_exist_ok=True, ignore=shutil.ignore_patterns("__pycache__"))
        else:
            shutil.copyfile(ROOT / name, source / name)
    current_version = json.loads((source / "tool_cli_contract.json").read_text())["tool_version"]
    next_version = "9.0.0"
    for name in ("src/worktree_controller/__init__.py", "src/worktree_controller/tool_cli_contract.json", "tool_cli_contract.json"):
        path = source / name
        path.write_bytes(path.read_bytes().replace(current_version.encode(), next_version.encode()))
    git(source, "add", ".")
    git(source, "commit", "-m", "next published release")
    target = git(source, "rev-parse", "HEAD")
    git(source, "tag", f"v{next_version}")
    root = tmp_path / "home/.local/share/worktree-controller"
    log = tmp_path / "home/.local/state/worktree-controller-rollout"
    log.mkdir(parents=True)
    governed = tmp_path / "governed"
    git(tmp_path, "init", "-b", "main", str(governed))
    consumer = tmp_path / "consumer"
    consumer.write_text("fixture\n")
    for name, value in {
        "CONTROLLER_REPO": source, "GIT_SOURCE": str(source), "ROOT": root,
        "VERSIONS": root / "versions", "ACTIVE": root / "active",
        "ACTIVATION_LOCK": root / "activation.lock", "LOG_DIR": log,
        "GOVERNED": (governed,), "SKILL_TARGET": tmp_path / "home/.agents/skills/worktree-lifecycle",
    }.items():
        monkeypatch.setattr(module, name, value)
    for oid, repo in ((previous, private), (target, source)):
        runtime = module.runtime_path(oid)
        package = runtime / "lib/python3.12/site-packages/worktree_controller"
        shutil.copytree(repo / "src/worktree_controller", package)
        binary = runtime / "bin/worktree-controller"
        binary.parent.mkdir()
        binary.write_text(f"#!{sys.executable}\nimport sys\nsys.path.insert(0, {str(package.parent)!r})\nfrom worktree_controller.cli import main\nraise SystemExit(main())\n")
        binary.chmod(0o755)
        (runtime / "uv-receipt.toml").write_text(f'source = "{source}?rev={oid}"\n')
    module.ACTIVE.symlink_to(module.runtime_path(previous))
    module.SKILL_TARGET.parent.mkdir(parents=True)
    shutil.copytree(source / "skills/worktree-lifecycle", module.SKILL_TARGET)
    module.bind_runtime_source(module.runtime_path(target), target)
    # The unit boundary records gate selection; opt-in rollout tests exercise actual clients.
    gates = []
    real_preflight = module.preflight
    monkeypatch.setattr(module, "preflight", lambda source, consumers, oid: gates.append(oid))
    args = SimpleNamespace(action="history-activate", version=target, request_id="original-request",
                           handover_baseline=baseline, previous_source_repo=private,
                           history_receipt=None, history_receipt_sha256=None, expected_plan_id=None,
                           consumer=[consumer], wait_ready_seconds=None, dry_run=True)
    return SimpleNamespace(module=module, source=source, private=private, previous=previous,
                           baseline=baseline, target=target, args=args, root=tmp_path, gates=gates,
                           real_preflight=real_preflight)


def invoke(item):
    work = item.root / f"work-{len(list(item.root.glob('work-*')))}"
    work.mkdir()
    return item.module.history_action(item.args, work)


def apply(item):
    plan, code = invoke(item)
    assert code == 0 and not plan["operation_complete"]
    item.args.expected_plan_id = plan["plan_id"]
    item.args.dry_run = False
    return invoke(item)


def receipt_mode(item, result, action="history-activate"):
    item.args.action = action
    item.args.history_receipt = Path(result["receipt"])
    item.args.history_receipt_sha256 = result["receipt_sha256"]
    item.args.dry_run = True


def test_exact_export_handover_repeat_and_explicit_rollback(handover):
    item = handover
    marker = item.root / "governed/unknown.bin"
    marker.write_bytes(b"\x00keep\xff")
    generated = item.root / "governed/generated"
    generated.mkdir()
    (generated / "part").write_bytes(b"generated evidence")
    # Reading a symlink may update atime. Force an old access time so the
    # read-only plan must preserve identity and contents without relying on it.
    os.utime(item.module.ACTIVE, ns=(0, item.module.ACTIVE.lstat().st_mtime_ns),
             follow_symlinks=False)
    original = item.module.ACTIVE.lstat()
    plan, code = invoke(item)
    assert code == 0 and plan["code"] == "HISTORY_HANDOVER_READY"
    assert not Path(plan["receipt"]).exists()
    observed = item.module.ACTIVE.lstat()
    assert observed[:7] == original[:7]
    assert (observed.st_mtime_ns, observed.st_ctime_ns) == (original.st_mtime_ns, original.st_ctime_ns)
    assert item.module.ACTIVE.readlink() == item.module.runtime_path(item.previous)
    result, code = apply(item)
    assert code == 0 and result["operation_complete"] and result["activation_performed"]
    assert item.module.active_version() == item.target
    receipt_mode(item, result)
    repeated, code = apply(item)
    assert code == 0 and repeated["operation_complete"] and not repeated["activation_performed"]
    receipt_mode(item, result, "history-rollback")
    rolled, code = apply(item)
    assert code == 0 and rolled["code"] == "HISTORY_ROLLED_BACK"
    assert item.module.active_version() == item.previous
    assert marker.read_bytes() == b"\x00keep\xff" and (generated / "part").read_bytes() == b"generated evidence"
    assert item.module.runtime_path(item.target).is_dir()
    receipt_mode(item, result)
    with pytest.raises(item.module.Abort, match="explicit rollback"):
        invoke(item)


@pytest.mark.parametrize("drift", ["plan", "source", "scope", "pointer", "skill", "runtime", "tag"])
def test_drift_refuses_without_switching(handover, drift):
    item = handover
    plan, _ = invoke(item)
    item.args.dry_run = False
    item.args.expected_plan_id = plan["plan_id"]
    if drift == "plan":
        item.args.expected_plan_id = "0" * 64
    elif drift == "source":
        (item.private / "pyproject.toml").write_text("changed")
        git(item.private, "commit", "-am", "new source does not alter frozen predecessor")
        # Frozen input, not a mutable source HEAD, is authoritative.
        item.args.previous_source_repo = item.source
    elif drift == "scope":
        item.module.GOVERNED = ()
    elif drift == "pointer":
        item.module.ACTIVE.unlink()
        other = item.module.runtime_path("0" * 40)
        other.mkdir(parents=True)
        item.module.ACTIVE.symlink_to(other)
    elif drift == "skill":
        (item.module.SKILL_TARGET / "SKILL.md").write_text("unknown Skill")
    elif drift == "runtime":
        next(item.module.runtime_path(item.target).glob("lib/*/site-packages/worktree_controller/__init__.py")).write_text("tampered")
    else:
        git(item.source, "tag", "-d", "v9.0.0")
    before = item.module.ACTIVE.readlink()
    with pytest.raises((item.module.Abort, OSError, ValueError, subprocess.SubprocessError)):
        invoke(item)
    assert item.module.ACTIVE.readlink() == before


def test_original_request_and_receipt_identity_are_immutable(handover):
    item = handover
    result, _ = apply(item)
    original = Path(result["receipt"]).read_bytes()
    item.args.dry_run = True
    item.args.version = item.baseline
    with pytest.raises(item.module.Abort, match="request identity drift"):
        invoke(item)
    receipt_mode(item, result)
    item.args.history_receipt_sha256 = "0" * 64
    with pytest.raises(item.module.Abort, match="SHA256"):
        invoke(item)
    assert Path(result["receipt"]).read_bytes() == original


@pytest.mark.parametrize("path", ["src/worktree_controller/__init__.py", "scripts/controller-runtime", "skills/worktree-lifecycle/SKILL.md"])
def test_nonidentical_export_never_qualifies(handover, path):
    item = handover
    target = item.private / path
    target.write_bytes(target.read_bytes() + b"\n# changed export\n")
    git(item.private, "commit", "-am", "different predecessor")
    previous = git(item.private, "rev-parse", "HEAD")
    with pytest.raises(item.module.Abort, match="differs from the public baseline"):
        item.module.history_proof(previous, item.baseline, item.target, item.private)
    assert item.module.active_version() == item.previous


def test_storage_migration_cannot_be_combined_with_history_handover(handover):
    item = handover
    path = item.source / "tool_cli_contract.json"
    document = json.loads(path.read_bytes())
    document["storage_layout_version"] += 1
    path.write_text(json.dumps(document))
    git(item.source, "commit", "-am", "different storage")
    target = git(item.source, "rev-parse", "HEAD")
    with pytest.raises(item.module.Abort, match="storage, metadata or lease-format"):
        item.module.history_proof(item.previous, item.baseline, target, item.private)
    assert item.module.active_version() == item.previous


def test_concurrent_activation_lock_and_pending_unknown_are_preserved(handover):
    item = handover
    with item.module.ACTIVATION_LOCK.open("wb") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(item.module.Abort, match="another activation"):
            invoke(item)
    pending = item.module.ROOT / ".active.pending"
    pending.write_bytes(b"unknown owner bytes")
    with pytest.raises(item.module.Abort, match="unknown pending runtime"):
        apply(item)
    assert pending.read_bytes() == b"unknown owner bytes"
    assert item.module.active_version() == item.previous


def test_interrupted_receipt_keeps_every_byte_and_retries_same_request(handover):
    item = handover
    proof = item.module.history_proof(item.previous, item.baseline, item.target, item.private)
    data = {"schema_version": 1, "contract": item.module.HISTORY_CONTRACT,
            "request_id": item.args.request_id, "proof": proof,
            "governed": [str(item.module.GOVERNED[0])], "consumers": [str(item.args.consumer[0])]}
    path = item.module.history_path(item.args.request_id)
    partial = item.module.source_document_bytes(data)[:41]
    pending = path.with_name(f".{path.name}.layout-pending")
    pending.write_bytes(partial)
    pending.chmod(0o600)
    result, code = apply(item)
    assert code == 0 and result["operation_complete"]
    preserved = path.with_name(path.name + f".preserved-{item.module.digest(partial)}")
    assert preserved.read_bytes() == partial


def test_post_activation_failure_is_failure_even_after_verified_restoration(handover, monkeypatch):
    item = handover
    def failed(*args):
        raise item.module.Abort("post-check failed")
    monkeypatch.setattr(item.module, "post_activation_check", failed)
    result, code = apply(item)
    assert code == 3 and not result["operation_complete"]
    assert result["code"] == "HISTORY_ACTIVATION_FAILED_PREDECESSOR_RESTORED"
    assert item.module.active_version() == item.previous
    assert item.gates[-1] == item.previous


def test_runtime_source_binding_is_primary_frozen_and_immutable(handover):
    item = handover
    document = item.module.runtime_source_document(item.target)
    assert document["source_repository"] == str(item.source)
    assert document["root_commit"] == item.baseline
    path = item.module.runtime_path(item.target) / "controller-source.json"
    assert json.loads(path.read_bytes()) == document
    raw = path.read_bytes()
    path.write_bytes(raw + b" ")
    with pytest.raises(item.module.Abort, match="binding differs"):
        item.module.bind_runtime_source(path.parent, item.target)
    assert path.read_bytes() == raw + b" "


@pytest.mark.parametrize("kind", ["writable", "symlink"])
def test_runtime_source_binding_rejects_unsafe_existing_record(handover, kind):
    item = handover
    path = item.module.runtime_path(item.target) / "controller-source.json"
    if kind == "writable":
        path.chmod(0o666)
    else:
        preserved = path.with_name("original-source.json")
        path.rename(preserved)
        path.symlink_to(preserved)
    with pytest.raises((item.module.Abort, item.module.LayoutError)):
        item.module.bind_runtime_source(path.parent, item.target)
    assert item.module.active_version() == item.previous


def test_history_cli_emits_one_json_decision_and_keeps_pointer(handover, monkeypatch, capsys):
    item = handover
    monkeypatch.setattr(sys, "argv", ["controller-activate", "history-activate", "--version", item.target,
                                     "--request-id", item.args.request_id, "--handover-baseline", item.baseline,
                                     "--previous-source-repo", str(item.private),
                                     "--consumer", str(item.args.consumer[0]), "--dry-run"])
    assert item.module.main() == 0
    streams = capsys.readouterr()
    decision = json.loads(streams.out)
    assert decision["code"] == "HISTORY_HANDOVER_READY" and not decision["operation_complete"]
    assert item.module.active_version() == item.previous
    assert "log:" in streams.err


def test_real_rollout_gate_preserves_live_writer_and_rejects_bad_consumer(handover, monkeypatch):
    from tests.test_controlled import acquire_one, adopt_canonical
    from tests.test_shadow import GitRepository
    from tests.test_writer_lease import POLICY_V6
    from worktree_controller import writer_lease

    item = handover
    module = item.module
    # Restore the production gate instead of the fixture's unit boundary.
    monkeypatch.setattr(module, "preflight", item.real_preflight)
    home = Path.home()
    launcher = (item.source / "scripts/controller-runtime").read_bytes()
    for relative in ("bin/controller-runtime", ".local/bin/worktree-controller", ".local/bin/worktree-session", ".local/bin/worktree-session-end"):
        path = home / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(launcher)
        path.chmod(0o755)
    consumer = item.args.consumer[0]
    capability = {"writer_session_version": 2, "holder_aware_write_guard": True,
                  "host_managed_writer_session_version": 2, "active_writer_sessions": 1,
                  "active_writer_session_versions": [2], "controller_activation_compatible": True}
    consumer.write_text(f"#!{sys.executable}\nprint({json.dumps(capability)!r})\n")
    consumer.chmod(0o755)
    with GitRepository(linked=0, policy_text=POLICY_V6) as repo:
        adopt_canonical(repo)
        path, _ = acquire_one(repo, task="live-history")
        holder = writer_lease.describe_holder(os.getpid(), client_id="manual")
        writer_lease.acquire_writer_lease(repo.repo, target=path, holder=holder,
                                          owner="module-owner", client_id="manual", holder_pid=holder.pid)
        module.GOVERNED = (repo.repo,)
        unknown = path / "unknown.bin"
        unknown.write_bytes(b"\x00preserve\xff")
        generated = path / "generated"
        generated.mkdir()
        (generated / "result").write_bytes(b"keep generated output")
        state = repo.repo / ".git/worktree-controller/v1"
        frozen = {str(p.relative_to(state)): p.read_bytes() for p in state.rglob("*.json")}
        consumer.write_text(f"#!{sys.executable}\nprint('{{}}')\n")
        with pytest.raises(module.RolloutBlocked):
            invoke(item)
        assert module.active_version() == item.previous
        consumer.write_text(f"#!{sys.executable}\nprint({json.dumps(capability)!r})\n")
        result, code = apply(item)
        assert code == 0 and result["operation_complete"]
        assert {str(p.relative_to(state)): p.read_bytes() for p in state.rglob("*.json")} == frozen
        receipt_mode(item, result, "history-rollback")
        rolled, code = apply(item)
        assert code == 0 and rolled["operation_complete"]
        assert unknown.read_bytes() == b"\x00preserve\xff"
        assert (generated / "result").read_bytes() == b"keep generated output"
        assert {str(p.relative_to(state)): p.read_bytes() for p in state.rglob("*.json")} == frozen
        writer_lease.release_writer_lease(repo.repo, target=path, holder=holder)


@pytest.mark.parametrize("action", ["stage", "activate", "rollback", "bootstrap"])
def test_unsupported_dry_run_cannot_mutate_any_runtime(tmp_path, monkeypatch, action):
    module = load_activate()
    monkeypatch.setattr(sys, "argv", ["controller-activate", action, "--version", "a" * 40, "--dry-run"])
    monkeypatch.setattr(module, "ROOT", tmp_path / "never-created")
    with pytest.raises(SystemExit) as failure:
        module.main()
    assert failure.value.code == 2 and not module.ROOT.exists()
