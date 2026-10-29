import json
import os

import pytest

from tests.test_controlled import acquire_one, adopt_canonical, handoff_one
from tests.test_shadow import GitRepository, git
from worktree_controller import build_resources as build
from worktree_controller import lifecycle
from worktree_controller.models import ControllerError
from worktree_controller.store import MetadataStore


@pytest.fixture
def project(monkeypatch):
    monkeypatch.setattr(lifecycle, "_validate_runtime_clear", lambda *a, **k: None)
    monkeypatch.setattr(lifecycle, "_typed_runtime_evidence", lambda *a: {
        "managed_references": [], "observed_references": [],
        "managed_visibility_uncertainties": [], "scan_uncertainties": [],
    })
    with GitRepository(linked=0) as repo:
        root = repo.repo / "apps/docs"
        root.mkdir(parents=True)
        (root / "package.json").write_text(json.dumps({"name": "docs", "scripts": {"build": "astro build"}}))
        (root / "package-lock.json").write_text(json.dumps({"name": "docs", "lockfileVersion": 3}))
        names = (".astro", ".generated", "dist", "node_modules")
        (repo.repo / ".gitignore").write_text("\n".join(f"apps/docs/{n}/" for n in names) + "\nevidence/\n")
        git(repo.repo, "add", ".")
        git(repo.repo, "commit", "-m", "synthetic build inputs")
        adopt_canonical(repo)
        target, _ = acquire_one(repo, role="hotfix")
        handoff_one(repo, target)
        paths = tuple(f"apps/docs/{n}" for n in names)
        for name in paths:
            (target / name).mkdir()
            (target / name / "output").write_bytes(b"generated")
        options = dict(target=target, owner="module-owner", expected_record_version=2,
                       expected_head=git(target, "rev-parse", "HEAD"), project="apps/docs",
                       recipe="build", paths=paths, attest_rebuildable=True)
        yield repo, target, options


def registered(repo, options):
    preview = build.register(repo.repo, **options)["decision"]
    return build.register(repo.repo, **options, dry_run=False,
                          expected_plan_id=preview["plan_id"])["decision"]


def clean(repo, target, **options):
    return lifecycle.cleanup_typed_disposable(repo.repo, target=target, owner="module-owner",
                                              expected_record_version=3, **options)["decision"]


def test_explicit_outputs_share_typed_transaction_and_preserve_external_hardlink(project):
    repo, target, options = project
    outside = repo.root / "external"
    os.link(target / options["paths"][0] / "output", outside)
    refs = git(repo.repo, "show-ref")
    assert registered(repo, options)["record_version"] == 3
    plan = clean(repo, target, dry_run=True)["plan"]
    assert not plan["blockers"]
    assert set(plan["cleanup_paths"]) == set(options["paths"])
    result = clean(repo, target, dry_run=False, expected_plan_id=plan["plan_id"])
    assert result["code"] == "TYPED_DISPOSABLE_CLEANED"
    assert outside.read_bytes() == b"generated" and outside.stat().st_nlink == 1
    assert all(not (target / p).exists() for p in options["paths"])
    assert git(repo.repo, "show-ref") == refs
    assert lifecycle.release(repo.repo, target=target, expected_record_version=3,
                             owner_release="module-owner")["decision"]["code"] == "RELEASED"


def test_unknown_research_evidence_still_blocks(project):
    repo, target, options = project
    registered(repo, options)
    (target / "evidence").mkdir()
    (target / "evidence/result").write_bytes(b"irreplaceable")
    plan = clean(repo, target, dry_run=True)["plan"]
    assert any(b["code"] == "UNKNOWN_IGNORED_RESOURCE" for b in plan["blockers"])
    assert not plan["cleanup_paths"]
    assert (target / "evidence/result").read_bytes() == b"irreplaceable"


@pytest.mark.parametrize("field,value,code", [
    ("owner", "another", "OWNER_MISMATCH"),
    ("expected_head", "0" * 40, "HEAD_MISMATCH"),
    ("expected_record_version", 9, "CAS_MISMATCH"),
    ("attest_rebuildable", False, "BUILD_RESOURCE_INVALID"),
    ("paths", ("evidence",), "BUILD_RESOURCE_INVALID"),
    ("recipe", "unknown", "BUILD_RESOURCE_INVALID"),
])
def test_registration_refuses_wrong_authority_or_scope(project, field, value, code):
    repo, _, options = project
    with pytest.raises(ControllerError) as error:
        build.register(repo.repo, **{**options, field: value})
    assert error.value.code == code


def test_registration_plan_rejects_replaced_root(project):
    repo, target, options = project
    plan = build.register(repo.repo, **options)["decision"]
    original = target / options["paths"][0]
    original.rename(original.with_name("saved"))
    original.mkdir()
    with pytest.raises(ControllerError):
        build.register(repo.repo, **options, dry_run=False, expected_plan_id=plan["plan_id"])


def test_cleanup_rejects_content_drift_after_plan(project):
    repo, target, options = project
    registered(repo, options)
    plan = clean(repo, target, dry_run=True)["plan"]
    leaf = target / options["paths"][0] / "output"
    metadata = leaf.stat()
    leaf.write_bytes(b"different")
    os.utime(leaf, ns=(metadata.st_atime_ns, metadata.st_mtime_ns))
    with pytest.raises(ControllerError) as error:
        clean(repo, target, dry_run=False, expected_plan_id=plan["plan_id"])
    assert error.value.code == "RESOURCE_PLAN_MISMATCH"
    assert leaf.read_bytes() == b"different"


def test_symlink_output_never_registers(project):
    repo, target, options = project
    path = target / options["paths"][0]
    path.rename(path.with_name("saved"))
    path.symlink_to(path.with_name("saved"), target_is_directory=True)
    with pytest.raises(ControllerError):
        build.register(repo.repo, **options)


def test_public_cli_preview_and_registration(project, capsys):
    from worktree_controller.cli import main

    repo, target, options = project
    args = ["build-resource-register", "--repo", str(repo.repo), "--path", str(target),
            "--owner", options["owner"], "--expected-record-version", "2",
            "--expected-head", options["expected_head"], "--project", "apps/docs",
            "--recipe", "build", "--attest-rebuildable", "--json"]
    for path in options["paths"]:
        args.extend(["--resource-path", path])
    assert main([*args, "--dry-run"]) == 0
    preview = json.loads(capsys.readouterr().out)["decision"]
    assert preview["code"] == "BUILD_RESOURCE_PLAN"
    assert main([*args, "--expected-plan-id", preview["plan_id"]]) == 0
    assert json.loads(capsys.readouterr().out)["decision"]["code"] == build.EVENT
    assert main([*args, "--expected-plan-id", preview["plan_id"]]) == 2
    assert json.loads(capsys.readouterr().out)["errors"][0]["code"] == "CAS_MISMATCH"


def test_failed_action_keeps_original_transaction_and_surviving_outputs(project, monkeypatch):
    repo, target, options = project
    registered(repo, options)
    plan = clean(repo, target, dry_run=True)["plan"]
    original = lifecycle.delete_typed_resource
    calls = []

    def fail_second(root, path, evidence):
        calls.append(path)
        if len(calls) == 2:
            raise ControllerError("RESOURCE_PLAN_MISMATCH", "synthetic concurrent change")
        return original(root, path, evidence)

    monkeypatch.setattr(lifecycle, "delete_typed_resource", fail_second)
    with pytest.raises(ControllerError) as error:
        clean(repo, target, dry_run=False, expected_plan_id=plan["plan_id"])
    assert error.value.code == "RESOURCE_PLAN_MISMATCH"
    assert not (target / calls[0]).exists()
    assert (target / calls[1] / "output").read_bytes() == b"generated"
    store = MetadataStore(repo.repo / ".git")
    assert list(store.transactions_dir.glob("*.json"))
    blocked = clean(repo, target, dry_run=True)["plan"]
    assert blocked["blockers"]


def test_registration_rejects_runtime_reference(project, monkeypatch):
    repo, _, options = project
    monkeypatch.setattr(lifecycle, "_typed_runtime_evidence", lambda *a: {
        "managed_references": [{"kind": "process", "identity": "synthetic"}],
        "observed_references": [], "managed_visibility_uncertainties": [], "scan_uncertainties": [],
    })
    with pytest.raises(ControllerError):
        build.register(repo.repo, **options)
