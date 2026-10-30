"""Registered outputs retain exact preservation and partial-action lineage."""

import hashlib
from pathlib import Path

import pytest

from tests.test_build_resources import clean, project, registered  # noqa: F401
from worktree_controller import lifecycle
from worktree_controller import multiple_cleanup_recovery as recovery
from worktree_controller import preservation as p
from worktree_controller.models import ControllerError
from worktree_controller.store import MetadataStore


def test_old_generation_survives_registration_and_typed_cleanup(project, tmp_path, monkeypatch):  # noqa: F811
    repo, target, options = project
    monkeypatch.setattr(p, "evidence_root", lambda: tmp_path / "preservations")
    store = MetadataStore(repo.repo / ".git")
    allocation = next(r.allocation_id for r in store.load().records.values()
                      if r.owner == "module-owner")
    copied = p.copy_preservation(p.plan_preservation(
        repo.repo, target, allocation, options["owner"], 2,
        source_relative=options["paths"][0],
    ))
    receipt = Path(copied["receipt"])
    original = receipt.read_bytes()
    registered(repo, options)
    plan = clean(repo, target, dry_run=True)["plan"]
    assert plan["blockers"] == []
    result = clean(repo, target, dry_run=False, expected_plan_id=plan["plan_id"])
    assert result["code"] == "TYPED_DISPOSABLE_CLEANED"
    assert all(not (target / path).exists() for path in options["paths"])
    assert receipt.read_bytes() == original


@pytest.fixture
def partial(project, tmp_path, monkeypatch):  # noqa: F811
    repo, target, options = project
    with (repo.repo / ".git/info/exclude").open("a") as stream:
        stream.write("\n.ruff_cache/\n__pycache__/\n")
    for relative in (".ruff_cache", "scripts/docs/__pycache__"):
        (target / relative).mkdir(parents=True)
        (target / relative / "cache").write_bytes(b"existing release-policy cache")
    monkeypatch.setattr(p, "evidence_root", lambda: tmp_path / "preservations")
    store = MetadataStore(repo.repo / ".git")
    allocation = next(r.allocation_id for r in store.load().records.values() if r.owner == "module-owner")
    copied = p.copy_preservation(p.plan_preservation(repo.repo, target, allocation,
        options["owner"], 2, source_relative=options["paths"][0]))
    registered(repo, options)
    with monkeypatch.context() as patch:
        from worktree_controller import build_preservation

        # Installed 1.33.1 has no registration-to-generation bridge.
        patch.setattr(build_preservation, "generations", lambda *a, **k: ([], []))
        plan = clean(repo, target, dry_run=True)["plan"]
        with pytest.raises(ControllerError, match="immutable receipt"):
            clean(repo, target, dry_run=False, expected_plan_id=plan["plan_id"])
    journal, = store.transactions_dir.glob("*.json")
    receipt = store.completion_receipts_dir / journal.name
    historical = [journal, receipt, Path(copied["receipt"]),
                  *list((store.cleanup_actions_dir / journal.stem).glob("*.json"))]
    before = {path: path.read_bytes() for path in historical}
    ctx = recovery.context(journal.stem, hashlib.sha256(before[journal]).hexdigest(),
                           hashlib.sha256(before[receipt]).hexdigest())
    args = dict(target=target, owner=options["owner"], version=3, recovery=ctx)
    return repo, target, options, store, args, before, journal


def test_partial_prefix_recovery_keeps_history_and_only_runs_suffix(partial):
    repo, target, options, store, args, before, journal = partial
    planned = recovery.prepare(repo.repo, **args, dry_run=True)["decision"]["plan"]
    assert planned["cleanup_paths"] == sorted(options["paths"])[1:]
    ref = recovery.prepare(repo.repo, **args, dry_run=False,
                           expected_plan_id=planned["plan_id"])["decision"]["recovery_receipt"]
    result = recovery.resume(repo.repo, recovery_path=Path(ref["path"]),
                             recovery_sha=ref["sha256"], dry_run=False)
    assert result["decision"]["code"] == "MULTIPLE_RECOVERY_CLEANED"
    assert all(path.read_bytes() == content for path, content in before.items())
    lifecycle.release(repo.repo, target=target, expected_record_version=3, owner_release=options["owner"])
    from worktree_controller.transaction_scope import terminal_state

    with store.lock(exclusive=False):
        assert terminal_state(store, journal.stem, verify_generations=True) == "RECOVERY_CLEANED"


@pytest.mark.parametrize("change", ["owner", "cas", "journal", "receipt", "action", "reappeared",
                                    "pending-bytes", "unknown", "generation", "head"])
def test_recovery_refuses_drift_before_mutation(partial, change):
    from tests.test_shadow import git

    repo, target, options, store, args, before, journal = partial
    pending = target / sorted(options["paths"])[1] / "output"
    if change == "owner":
        args["owner"] = "other-owner"
    elif change == "cas":
        args["version"] = 2
    elif change in {"journal", "receipt"}:
        key = "original_transaction_sha256" if change == "journal" else "original_cleanup_receipt_sha256"
        args["recovery"] = {**args["recovery"], key: "0" * 64}
    elif change == "action":
        (store.cleanup_actions_dir / journal.stem / "0001-before.json").write_text("{}")
    elif change == "reappeared":
        (target / sorted(options["paths"])[0]).mkdir()
    elif change == "pending-bytes":
        pending.write_bytes(b"concurrent change")
    elif change == "unknown":
        (target / "evidence").mkdir()
        (target / "evidence/research").write_bytes(b"unique")
    elif change == "generation":
        copied = next(path for path in before if path.name == "receipt.json")
        (copied.parent / "tree/output").write_bytes(b"changed preservation")
    elif change == "head":
        (target / "tracked-new").write_text("changed")
        git(target, "add", "tracked-new")
        git(target, "commit", "-m", "synthetic drift")
    expected = pending.read_bytes()
    with pytest.raises(ControllerError):
        recovery.prepare(repo.repo, **args, dry_run=True)
    assert pending.read_bytes() == expected
    assert all(path.read_bytes() == content for path, content in before.items())


def test_public_recovery_is_plan_bound_and_repeat_safe(partial, capsys):
    import json

    from worktree_controller.cli import main

    repo, target, _, _, args, before, _ = partial
    context = args["recovery"]
    command = ["resource-recover", "--repo", str(repo.repo), "--json",
               "--stage", "prepare-multiple", "--path", str(target),
               "--owner", args["owner"], "--expected-record-version", "3",
               "--transaction-id", context["original_transaction_id"],
               "--transaction-sha256", context["original_transaction_sha256"],
               "--cleanup-receipt-sha256", context["original_cleanup_receipt_sha256"]]

    def invoke(arguments, code=0):
        assert main(arguments) == code
        return json.loads(capsys.readouterr().out)

    plan = invoke([*command, "--dry-run"])["decision"]["plan"]
    invoke([*command, "--expected-plan-id", "0" * 64], 2)
    ref = invoke([*command, "--expected-plan-id", plan["plan_id"]])["decision"]["recovery_receipt"]
    invoke([*command, "--expected-plan-id", plan["plan_id"]], 2)
    resume = ["resource-recover", "--repo", str(repo.repo), "--json", "--stage", "resume",
              "--recovery-receipt", ref["path"], "--recovery-receipt-sha256", ref["sha256"]]
    invoke([*resume, "--dry-run"])
    cleaned = invoke(resume)["decision"]
    repeated = invoke(resume)["decision"]
    assert cleaned["receipt_id"] == repeated["receipt_id"]
    assert all(path.read_bytes() == content for path, content in before.items())


def test_preflight_refuses_replaced_preserved_output_before_any_delete(project, tmp_path, monkeypatch):  # noqa: F811
    repo, target, options = project
    monkeypatch.setattr(p, "evidence_root", lambda: tmp_path / "preservations")
    store = MetadataStore(repo.repo / ".git")
    allocation = next(r.allocation_id for r in store.load().records.values() if r.owner == "module-owner")
    p.copy_preservation(p.plan_preservation(repo.repo, target, allocation,
        options["owner"], 2, source_relative=options["paths"][0]))
    output = target / options["paths"][0]
    output.rename(tmp_path / "preserved-original")
    output.mkdir()
    (output / "output").write_bytes(b"generated")
    registered(repo, options)
    plan = clean(repo, target, dry_run=True)["plan"]
    assert plan["blockers"] and plan["cleanup_paths"] == []
    assert all((target / path / "output").read_bytes() == b"generated" for path in options["paths"])
    assert not list(store.transactions_dir.glob("*.json"))
