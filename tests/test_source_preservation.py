from __future__ import annotations

import copy
import hashlib
import json
import os
import stat
from pathlib import Path

import pytest

from tests.test_controlled import acquire_one, adopt_canonical
from tests.test_retention_admission import POLICY_V5
from tests.test_shadow import GitRepository, git
from tests.test_typed_disposable import empty_runtime_evidence
from worktree_controller import lifecycle
from worktree_controller import source_preservation as s
from worktree_controller.models import ControllerError


@pytest.fixture
def source(monkeypatch):
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setattr(
        lifecycle, "runtime_authority_evidence", lambda *a, **k: empty_runtime_evidence()
    )
    with GitRepository(linked=0, policy_text=POLICY_V5) as repository:
        root = repository.repo
        (root / "src").mkdir()
        (root / "src/plain.py").write_bytes(b"before\n")
        (root / "src/binary.bin").write_bytes(b"\x00\xffbefore\xfe")
        (root / "src/mode.sh").write_bytes(b"unchanged bytes\n")
        (root / "frontend").mkdir()
        (root / "frontend/package.json").write_text(
            json.dumps(
                {
                    "name": "fixture",
                    "scripts": {"build": "vite build"},
                    "devDependencies": {"vite": "1.0.0"},
                }
            )
        )
        (root / "frontend/index.html").write_text("<html></html>")
        (root / "frontend/package-lock.json").write_text(
            json.dumps(
                {
                    "name": "fixture",
                    "lockfileVersion": 3,
                    "packages": {
                        "": {"name": "fixture", "devDependencies": {"vite": "1.0.0"}},
                        "node_modules/vite": {"version": "1.0.0"},
                    },
                }
            )
        )
        (root / ".gitignore").write_text("ignored/\nfrontend/dist/\n")
        git(root, "add", ".")
        git(root, "commit", "-m", "source baseline")
        adopt_canonical(repository)
        target, acquired = acquire_one(repository)
        (target / "src/plain.py").write_bytes(b"after\n")
        (target / "src/binary.bin").write_bytes(b"\x00\xffafter\xfe\x80")
        (target / "src/mode.sh").chmod(0o751)
        allocation = acquired["decision"]["allocation_id"]
        state = repository.root / "evidence"
        monkeypatch.setattr(s.p, "evidence_root", lambda: state)
        args = (root, target, allocation, "module-owner", 1)
        kwargs = {"expected_head": git(target, "rev-parse", "HEAD"), "state_root": state}
        yield repository, target, state, args, kwargs


def planned(source):
    _, _, _, args, kwargs = source
    return s.plan_preservation(*args, **kwargs)


def copied(source):
    plan = planned(source)
    result = s.copy_preservation(plan, state_root=source[2])
    return plan, result


def verify(source, plan, result):
    return s.verify_preservation(plan, result["receipt_sha256"], state_root=source[2])


def no_receipt(plan):
    assert not (Path(plan["destination"]) / "receipt.json").exists()


def test_text_binary_mode_exact_overlay_preserved_without_index_or_source_writes(source):
    _, target, _, _, _ = source
    before = {str(p): p.read_bytes() for p in (target / "src").iterdir()}
    status = git(target, "status", "--porcelain")
    plan, result = copied(source)
    assert result["file_count"] == 3
    assert verify(source, plan, result)["verification"] == "PASS"
    assert git(target, "status", "--porcelain") == status
    assert {str(p): p.read_bytes() for p in (target / "src").iterdir()} == before
    assert (
        s._scan(plan["authority"]["identity"])["index_sha256"]
        == plan["source_tree"]["index_sha256"]
    )
    tree = Path(result["generation"]) / "tree"
    for row in plan["source_tree"]["files"]:
        assert (tree / row["path"]).read_bytes() == (target / row["path"]).read_bytes()
        assert row["mode"] == stat.S_IMODE((target / row["path"]).stat().st_mode)
        assert stat.S_IMODE((tree / row["path"]).stat().st_mode) == 0o400
    # Compare the independently reconstructed tree with real Git, only in this
    # disposable fixture after the source/index non-mutation assertions above.
    git(target, "add", "src")
    assert git(target, "write-tree") == plan["source_tree"]["reconstructed_tree_oid"]
    receipt = json.loads(Path(result["receipt"]).read_bytes())
    assert receipt["cleanup_authorized"] is False
    assert receipt["tree_sha256"] == plan["source_tree"]["tree_sha256"]


@pytest.mark.parametrize("kind", ["staged", "untracked", "deleted", "symlink", "assume-unchanged"])
def test_unsupported_git_states_block(source, kind):
    _, target, _, _, _ = source
    path = target / "src/plain.py"
    if kind == "staged":
        git(target, "add", "src/plain.py")
    elif kind == "untracked":
        (target / "new.py").write_text("new")
    elif kind == "deleted":
        path.unlink()
    elif kind == "symlink":
        path.unlink()
        path.symlink_to("binary.bin")
    else:
        git(target, "update-index", "--assume-unchanged", "src/plain.py")
    with pytest.raises(ControllerError):
        planned(source)


def test_wrong_expected_head_blocks(source):
    *_, args, kwargs = source
    with pytest.raises(ControllerError, match="guard refused") as error:
        s.plan_preservation(*args, **{**kwargs, "expected_head": "0" * 40})
    assert error.value.code == "SOURCE_HEAD_MISMATCH"


def test_head_changes_after_plan_block(source):
    _, target, _, _, _ = source
    plan = planned(source)
    git(target, "commit", "--allow-empty", "-m", "head changed")
    with pytest.raises(ControllerError):
        s.copy_preservation(plan, state_root=source[2])
    no_receipt(plan)


@pytest.mark.parametrize("kind", ["content", "mode", "inode"])
def test_source_change_after_plan_blocks(source, kind):
    _, target, _, _, _ = source
    plan = planned(source)
    path = target / "src/plain.py"
    if kind == "content":
        path.write_bytes(b"different\n")
    elif kind == "mode":
        path.chmod(0o600)
    else:
        data = path.read_bytes()
        path.rename(target.parent / "old-inode")
        path.write_bytes(data)
    with pytest.raises(ControllerError):
        s.copy_preservation(plan, state_root=source[2])
    no_receipt(plan)


def test_source_mutation_during_copy_has_no_success_receipt(source, monkeypatch):
    _, target, _, _, _ = source
    plan = planned(source)
    original = s._copy_files

    def race(*args, **kwargs):
        original(*args, **kwargs)
        (target / "src/plain.py").write_bytes(b"changed during copy\n")

    monkeypatch.setattr(s, "_copy_files", race)
    with pytest.raises(ControllerError):
        s.copy_preservation(plan, state_root=source[2])
    no_receipt(plan)


@pytest.mark.parametrize("kind", ["parent", "leaf"])
def test_source_path_swap_at_fd_open_never_reads_external_target(source, monkeypatch, kind):
    repository, target, _, _, _ = source
    plan = planned(source)
    outside = repository.root / "outside"
    outside.mkdir()
    sentinel = outside / "plain.py"
    sentinel.write_bytes(b"external sentinel")
    sentinel_inode = sentinel.stat().st_ino
    real_open, real_read = os.open, os.read
    injected = False
    armed = False
    original_copy = s._copy_files

    def copying(*args, **kwargs):
        nonlocal armed
        armed = True
        return original_copy(*args, **kwargs)

    def race(name, flags, *args, **kwargs):
        nonlocal injected
        if armed and not injected and name == "plain.py" and flags & os.O_PATH:
            injected = True
            if kind == "parent":
                (target / "src").rename(repository.root / "original-src")
                (target / "src").symlink_to(outside, target_is_directory=True)
            else:
                (target / "src/plain.py").rename(repository.root / "original-file")
                (target / "src/plain.py").symlink_to(sentinel)
        return real_open(name, flags, *args, **kwargs)

    def read(fd, size):
        assert os.fstat(fd).st_ino != sentinel_inode, "external source target was read"
        return real_read(fd, size)

    monkeypatch.setattr(s, "_copy_files", copying)
    monkeypatch.setattr(os, "open", race)
    monkeypatch.setattr(os, "read", read)
    with pytest.raises((ControllerError, OSError)):
        s.copy_preservation(plan, state_root=source[2])
    assert injected
    no_receipt(plan)
    assert sentinel.read_bytes() == b"external sentinel"


def test_destination_parent_race_external_tree_unchanged(source, monkeypatch):
    repository, _, state, _, _ = source
    state.mkdir()
    plan = planned(source)
    outside = repository.root / "outside"
    outside.mkdir()
    (outside / "sentinel").write_bytes(b"external sentinel")
    original = os.mkdir
    injected = False

    def race(name, mode=0o777, *, dir_fd=None):
        nonlocal injected
        if name == plan["generation"] and not injected:
            injected = True
            state.rename(repository.root / "original-evidence")
            state.symlink_to(outside, target_is_directory=True)
        return original(name, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "mkdir", race)
    with pytest.raises((ControllerError, OSError)):
        s.copy_preservation(plan, state_root=state)
    assert injected
    assert sorted(p.name for p in outside.iterdir()) == ["sentinel"]
    assert (outside / "sentinel").read_bytes() == b"external sentinel"


@pytest.mark.parametrize("kind", ["missing", "content", "mode", "extra", "symlink"])
def test_preserved_tree_verification_rejects_tampering(source, kind):
    plan, result = copied(source)
    tree = Path(result["generation"]) / "tree"
    parent = tree / "src"
    parent.chmod(0o700)
    file = parent / "plain.py"
    if kind == "missing":
        file.unlink()
    elif kind == "content":
        file.chmod(0o600)
        file.write_bytes(b"changed bytes")
        file.chmod(0o400)
    elif kind == "mode":
        file.chmod(0o600)
    elif kind == "extra":
        (parent / "extra").write_text("unexpected")
    else:
        file.unlink()
        file.symlink_to(source[1] / "src/plain.py")
    parent.chmod(0o500)
    with pytest.raises((ControllerError, OSError)):
        verify(source, plan, result)


def test_generation_collision_and_repeat_never_overwrite(source):
    plan, result = copied(source)
    receipt = Path(result["receipt"]).read_bytes()
    with pytest.raises((ControllerError, OSError)):
        s.copy_preservation(plan, state_root=source[2])
    assert Path(result["receipt"]).read_bytes() == receipt
    with pytest.raises((ControllerError, OSError)):
        s.plan_preservation(*source[3], **source[4], generation=plan["generation"])


@pytest.mark.parametrize("name", ["plan.json", "manifest.json", "receipt.json"])
def test_changed_evidence_rejected_without_rewriting_receipt(source, name):
    plan, result = copied(source)
    path = Path(result["generation"]) / name
    path.chmod(0o600)
    value = json.loads(path.read_bytes())
    value["unexpected"] = True
    path.write_text(json.dumps(value))
    path.chmod(0o400)
    before = Path(result["receipt"]).read_bytes()
    with pytest.raises(ControllerError):
        verify(source, plan, result)
    assert Path(result["receipt"]).read_bytes() == before


def test_wrong_allocation_and_wrong_plan_block(source):
    plan, result = copied(source)
    altered = copy.deepcopy(plan)
    altered["authority"]["identity"]["allocation_id"] = "0" * 36
    with pytest.raises(ControllerError):
        verify(source, altered, result)
    with pytest.raises(ControllerError):
        s.plan_preservation(source[3][0], source[3][1], "0" * 36, "module-owner", 1, **source[4])


@pytest.mark.parametrize(
    "name", [".env", "credentials.json", "private.pem", "executor-home/config.json"]
)
def test_credential_path_rejected_before_payload_open(source, monkeypatch, name):
    _, target, _, _, _ = source
    path = target / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"synthetic credential fixture only")
    git(target, "add", name)
    git(target, "commit", "-m", "synthetic protected file")
    path.write_bytes(b"synthetic changed credential fixture only")
    original = s._read

    def protected(chain, relative):
        assert relative != name, "credential payload was opened"
        return original(chain, relative)

    monkeypatch.setattr(s, "_read", protected)
    with pytest.raises(ControllerError) as error:
        s.plan_preservation(
            *source[3], **{**source[4], "expected_head": git(target, "rev-parse", "HEAD")}
        )
    assert error.value.code == "SOURCE_CREDENTIAL_PATH"


def test_recognized_secret_marker_never_copied_or_printed(source, capsys):
    _, target, state, _, _ = source
    (target / "src/plain.py").write_bytes(
        b"-----BEGIN PRIVATE KEY-----\nsynthetic invalid fixture\n"
    )
    with pytest.raises(ControllerError) as error:
        planned(source)
    assert error.value.code == "SOURCE_SECRET_MATERIAL"
    assert not state.exists()
    assert "PRIVATE KEY" not in str(error.value)
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize(
    "field", ["managed_references", "observed_references", "managed_visibility_uncertainties"]
)
def test_runtime_authority_blocks_preservation(source, monkeypatch, field):
    evidence = empty_runtime_evidence()
    evidence[field] = [{"pid": 99999999, "kind": "fixture", "reference": "fixture"}]
    monkeypatch.setattr(lifecycle, "runtime_authority_evidence", lambda *a, **k: evidence)
    with pytest.raises(ControllerError):
        planned(source)


def test_runtime_uncertainty_preserves_the_actual_structured_refusal(source, monkeypatch):
    evidence = empty_runtime_evidence()
    evidence["scan_uncertainties"] = [{"code": "PID_IDENTITY_CHANGED", "pid": 99999999}]
    monkeypatch.setattr(lifecycle, "runtime_authority_evidence", lambda *a, **k: evidence)
    with pytest.raises(s.AuthorityBlocked) as error:
        planned(source)
    assert error.value.code == "RUNTIME_SCAN_UNCERTAIN"
    assert error.value.runtime_evidence == evidence


def test_host_gap_is_recorded_and_ignored_resources_are_not_claimed_covered(source, monkeypatch):
    _, target, _, _, _ = source
    evidence = empty_runtime_evidence()
    evidence["host_visibility_gaps"] = [
        {"pid": 99999999, "classification": "HOST_VISIBILITY_GAP", "reason": "absence not proven"}
    ]
    monkeypatch.setattr(lifecycle, "runtime_authority_evidence", lambda *a, **k: evidence)
    (target / "ignored").mkdir()
    (target / "ignored/unique-state").write_bytes(b"outside source coverage")
    plan, result = copied(source)
    assert verify(source, plan, result)["verification"] == "PASS"
    receipt = json.loads(Path(result["receipt"]).read_bytes())
    assert receipt["runtime_checks"][-1]["host_visibility_gaps"] == evidence["host_visibility_gaps"]
    assert not any(row["path"].startswith("ignored/") for row in plan["source_tree"]["files"])
    assert (target / "ignored/unique-state").read_bytes() == b"outside source coverage"


def test_source_changes_after_receipt_block_exact_verification(source):
    plan, result = copied(source)
    before = hashlib.sha256(Path(result["receipt"]).read_bytes()).hexdigest()
    (source[1] / "src/plain.py").write_bytes(b"later change\n")
    with pytest.raises(ControllerError):
        verify(source, plan, result)
    assert hashlib.sha256(Path(result["receipt"]).read_bytes()).hexdigest() == before


def test_input_line_ending_policy_keeps_exact_binary_and_text_bytes(source):
    git(source[1], "config", "core.autocrlf", "input")
    plan, result = copied(source)
    assert plan["source_tree"]["autocrlf"] == "input"
    assert verify(source, plan, result)["verification"] == "PASS"


@pytest.mark.parametrize("name", [".", "receipt.json", "manifest.json", "plan.json"])
def test_generation_readonly_modes_are_part_of_verification(source, name):
    plan, result = copied(source)
    path = Path(result["generation"]) / name
    path.chmod(0o700 if name == "." else 0o600)
    with pytest.raises(ControllerError) as error:
        verify(source, plan, result)
    assert error.value.code == "SOURCE_GENERATION_NOT_SEALED"


@pytest.mark.parametrize("name", ["receipt.json", "manifest.json", "plan.json"])
def test_document_bytes_are_immutable_even_when_json_value_is_unchanged(source, name):
    plan, result = copied(source)
    path = Path(result["generation"]) / name
    path.chmod(0o600)
    path.write_bytes(path.read_bytes() + b"\n")
    path.chmod(0o400)
    with pytest.raises(ControllerError) as error:
        verify(source, plan, result)
    assert error.value.code == "SOURCE_RECEIPT_INVALID"
