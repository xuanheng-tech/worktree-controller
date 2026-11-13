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


@pytest.fixture
def rebuilt(project, tmp_path, monkeypatch):  # noqa: F811
    from tests.test_controlled import handoff_one
    from tests.test_shadow import git

    repo, target, options = project
    monkeypatch.setattr(p, "evidence_root", lambda: tmp_path / "preservations")
    store = MetadataStore(repo.repo / ".git")
    allocation = next(r.allocation_id for r in store.load().records.values() if r.owner == "module-owner")
    relative = options["paths"][0]
    old = p.copy_preservation(p.plan_preservation(repo.repo, target, allocation,
        options["owner"], 2, source_relative=relative))
    old_receipt = Path(old["receipt"])
    original = old_receipt.read_bytes()
    lifecycle.reopen(repo.repo, target=target, owner=options["owner"], expected_record_version=2)
    lifecycle.park(repo.repo, target=target, owner=options["owner"], expected_record_version=3)
    lifecycle.resume(repo.repo, target=target, owner=options["owner"], expected_record_version=4)
    (target / relative).rename(tmp_path / "old-build")
    (target / relative).mkdir()
    (target / relative / "output").write_bytes(b"rebuilt bytes")
    (target / "source.txt").write_text("new committed source\n")
    git(target, "add", "source.txt")
    git(target, "commit", "-m", "new source")
    handoff_one(repo, target, expected=5)
    options.update(expected_record_version=6, expected_head=git(target, "rev-parse", "HEAD"))
    registered(repo, options)
    return repo, target, options, store, allocation, relative, old_receipt, original


@pytest.mark.parametrize("project", ["retained-feature"], indirect=True)
def test_rebuilt_source_requires_current_preservation_before_any_deletion(rebuilt):
    repo, target, options, _, _, _, receipt, original = rebuilt
    result = lifecycle.cleanup_typed_disposable(repo.repo, target=target, owner=options["owner"],
        expected_record_version=7, dry_run=True)["decision"]["plan"]
    assert result["blockers"] and result["cleanup_paths"] == []
    assert result["blockers"][0]["code"] == "BUILD_PRESERVATION_BASELINE_REQUIRED"
    assert receipt.read_bytes() == original


@pytest.mark.parametrize("project", ["retained-feature"], indirect=True)
def test_rebuilt_generation_cleanup_and_release_keep_old_evidence(rebuilt):
    repo, target, options, _, allocation, relative, receipt, original = rebuilt
    copied = p.copy_preservation(p.plan_preservation(repo.repo, target, allocation,
        options["owner"], 7, source_relative=relative))
    current_receipt = Path(copied["receipt"])
    current_bytes = current_receipt.read_bytes()
    arguments = dict(target=target, owner=options["owner"], expected_record_version=7)
    plan = lifecycle.cleanup_typed_disposable(repo.repo, **arguments, dry_run=True)["decision"]["plan"]
    assert plan["blockers"] == []
    result = lifecycle.cleanup_typed_disposable(repo.repo, **arguments, dry_run=False,
        expected_plan_id=plan["plan_id"])
    assert result["decision"]["code"] == "TYPED_DISPOSABLE_CLEANED"
    assert receipt.read_bytes() == original and current_receipt.read_bytes() == current_bytes
    assert lifecycle.release(repo.repo, target=target, expected_record_version=7,
        owner_release=options["owner"])["decision"]["code"] == "RELEASED"


@pytest.mark.parametrize("project", ["retained-feature"], indirect=True)
def test_rebuilt_generation_corrupt_old_copy_is_still_blocked(rebuilt):
    repo, target, options, _, allocation, relative, receipt, _ = rebuilt
    p.copy_preservation(p.plan_preservation(repo.repo, target, allocation,
        options["owner"], 7, source_relative=relative))
    (receipt.parent / "tree/output").write_bytes(b"corrupted old copy")
    plan = lifecycle.cleanup_typed_disposable(repo.repo, target=target, owner=options["owner"],
        expected_record_version=7, dry_run=True)["decision"]["plan"]
    assert plan["blockers"] and plan["cleanup_paths"] == []
    assert (target / relative / "output").read_bytes() == b"rebuilt bytes"


@pytest.mark.parametrize("project", ["retained-feature"], indirect=True)
@pytest.mark.parametrize("corrupt_copy", [False, True])
def test_absent_registered_rebuild_keeps_copy_without_claiming_cleanup(rebuilt, corrupt_copy):
    import shutil

    repo, target, options, store, allocation, _, receipt, original = rebuilt
    for relative in options["paths"]:
        shutil.rmtree(target / relative)
    if corrupt_copy:
        (receipt.parent / "tree/output").write_bytes(b"corrupted copy")
        with pytest.raises(ControllerError):
            lifecycle.release(repo.repo, target=target, expected_record_version=7,
                              owner_release=options["owner"], dry_run=True)
        assert target.exists()
        return
    args = dict(target=target, expected_record_version=7, owner_release=options["owner"])
    preview = lifecycle.release(repo.repo, **args, dry_run=True)["decision"]
    observations = preview["registered_build_absences"]
    assert {r["path"] for r in observations} == set(options["paths"])
    assert all(r["cleanup_attribution"] == "NOT_ESTABLISHED_BY_OBSERVATION" for r in observations)
    assert list(store.completion_receipts_dir.glob("*.json")) == []
    result = lifecycle.release(repo.repo, **args)["decision"]
    assert result["code"] == "RELEASED"
    assert result["registered_build_absences"] == observations
    assert store.load().records[allocation].history[-1]["registered_build_absences"] == observations
    assert receipt.read_bytes() == original
    assert list(store.completion_receipts_dir.glob("*.json")) == []


def test_absent_preserved_output_without_registration_still_blocks(project, tmp_path, monkeypatch):  # noqa: F811
    import shutil

    repo, target, options = project
    monkeypatch.setattr(p, "evidence_root", lambda: tmp_path / "preservations")
    store = MetadataStore(repo.repo / ".git")
    allocation = next(r.allocation_id for r in store.load().records.values() if r.owner == "module-owner")
    relative = options["paths"][0]
    p.copy_preservation(p.plan_preservation(repo.repo, target, allocation,
        options["owner"], 2, source_relative=relative))
    shutil.rmtree(target / relative)
    with pytest.raises(ControllerError, match="actual completed action evidence"):
        lifecycle.release(repo.repo, target=target, expected_record_version=2,
                          owner_release=options["owner"], dry_run=True)


@pytest.mark.parametrize("project", ["retained-feature"], indirect=True)
def test_registered_absence_survives_interrupted_release(rebuilt, monkeypatch):
    import shutil

    repo, target, options, store, allocation, _, receipt, original = rebuilt
    for relative in options["paths"]:
        shutil.rmtree(target / relative)

    def crash(name):
        if name == "release_worktree_removed":
            raise RuntimeError("simulated interruption")

    args = dict(target=target, expected_record_version=7, owner_release=options["owner"])
    with monkeypatch.context() as patch:
        patch.setattr(lifecycle, "_crash_point", crash)
        with pytest.raises(RuntimeError, match="simulated interruption"):
            lifecycle.release(repo.repo, **args)
    result = lifecycle.release(repo.repo, **args)["decision"]
    assert result["code"] == "RELEASE_RECOVERED"
    event = store.load().records[allocation].history[-1]
    assert {r["path"] for r in event["registered_build_absences"]} == set(options["paths"])
    assert receipt.read_bytes() == original


@pytest.mark.parametrize("project", ["retained-feature"], indirect=True)
@pytest.mark.parametrize("change", ["ancestor", "unknown_file"])
def test_registered_absence_does_not_cover_other_drift(rebuilt, change):
    import shutil

    repo, target, options, _, _, _, _, _ = rebuilt
    for relative in options["paths"]:
        shutil.rmtree(target / relative)
    if change == "ancestor":
        parent = target / "apps"
        previous = target / "previous-apps"
        parent.rename(previous)
        shutil.copytree(previous, parent)
        shutil.rmtree(previous)
    else:
        (target / "evidence").mkdir()
        (target / "evidence/result").write_bytes(b"unknown evidence")
    with pytest.raises(ControllerError):
        lifecycle.release(repo.repo, target=target, expected_record_version=7,
                          owner_release=options["owner"], dry_run=True)
    assert target.exists()


@pytest.mark.parametrize("project", ["retained-feature"], indirect=True)
@pytest.mark.parametrize("change", ["owner", "event", "history", "head", "registration"])
def test_rebuilt_lineage_refuses_changed_history(rebuilt, change):
    import json
    from dataclasses import replace

    from tests.test_shadow import git
    from worktree_controller.preserved_cleanup import reopened_source_identity

    _, target, _, store, allocation, _, receipt, _ = rebuilt
    record = store.load().records[allocation]
    previous = json.loads(receipt.read_bytes())["allocation_source_identity"]
    current = {**previous, "expected_record_version": 7,
               "expected_head": git(target, "rev-parse", "HEAD")}
    assert reopened_source_identity(previous, current, target, record)
    if change == "owner":
        current["owner"] = "foreign"
    elif change == "head":
        previous["expected_head"] = "0" * 40
    else:
        history = [dict(row) for row in record.history]
        if change == "event":
            history[3]["event"] = "UNKNOWN_TRANSITION"
        elif change == "history":
            history.pop(3)
        else:
            history[-1]["build_resources"] = {**history[-1]["build_resources"], "head": "0" * 40}
        record = replace(record, history=tuple(history))
    if change == "head":
        with pytest.raises(ControllerError, match="commit is not resolvable") as failure:
            reopened_source_identity(previous, current, target, record)
        assert failure.value.code == "INVALID_COMMIT"
    else:
        assert not reopened_source_identity(previous, current, target, record)


@pytest.mark.parametrize("preexisting_absence", [False, True])
def test_old_generation_survives_registration_and_typed_cleanup(
    project, tmp_path, monkeypatch, preexisting_absence,  # noqa: F811
):
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
    if preexisting_absence:
        import shutil

        shutil.rmtree(target / options["paths"][0])
    plan = clean(repo, target, dry_run=True)["plan"]
    assert plan["blockers"] == []
    if preexisting_absence:
        assert options["paths"][0] not in plan["cleanup_paths"]
        assert plan["registered_build_absences"][0]["path"] == options["paths"][0]
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
