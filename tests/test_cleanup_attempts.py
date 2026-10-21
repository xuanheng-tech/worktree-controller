import json
import os
import uuid
from pathlib import Path

import pytest

from tests.test_cleanup_recovery import interrupted as interrupted
from tests.test_cleanup_recovery import prepare, preserve
from tests.test_typed_disposable import empty_runtime_evidence
from tests.test_typed_disposable import target as target
from worktree_controller import cleanup_attempts as a
from worktree_controller import cleanup_evidence as e
from worktree_controller import cleanup_recovery as r
from worktree_controller import lifecycle
from worktree_controller.models import ControllerError
from worktree_controller.store import now_iso


@pytest.fixture(params=["missing-receipt", "refusal-receipt"])
def pending_typed(target, tmp_path, monkeypatch, request):
    from worktree_controller.git_backend import inventory
    from worktree_controller.store import MetadataStore

    repository, worktree, allocation = target
    alias = tmp_path / "external-build"
    os.link(worktree / "frontend/dist/assets/index.js", alias)
    store = MetadataStore(inventory(repository.repo).common_dir)
    options = dict(target=worktree, owner="module-owner", expected_record_version=2)
    plan = lifecycle.cleanup_typed_disposable(repository.repo, **options, dry_run=True)["decision"]["plan"]
    write = MetadataStore.write_transaction
    def interrupted_write(self, transaction, value):
        write(self, transaction, value)
        raise KeyboardInterrupt
    with monkeypatch.context() as patch:
        if request.param == "missing-receipt":
            patch.setattr(MetadataStore, "write_transaction", interrupted_write)
            error = KeyboardInterrupt
        else:
            original_plan = lifecycle._typed_cleanup_plan_locked
            refused = False
            def transient_refusal(*args, **kwargs):
                nonlocal refused
                result = original_plan(*args, **kwargs)
                if kwargs.get("check_transaction") is False and not refused:
                    refused = True
                    result["blockers"] = [{"code": "RUNTIME_REFERENCE", "message": "fixture transient process"}]
                return result
            patch.setattr(lifecycle, "_typed_cleanup_plan_locked", transient_refusal)
            error = ControllerError
        with pytest.raises(error) as failure:
            lifecycle.cleanup_typed_disposable(repository.repo, **options, dry_run=False,
                                              expected_plan_id=plan["plan_id"])
        if request.param == "refusal-receipt":
            assert failure.value.code == "RUNTIME_REFERENCE"
    journal = next(store.transactions_dir.glob("*.json"))
    _, digest = e.read(journal)
    return repository, worktree, allocation, store, journal, digest, options, alias


@pytest.mark.parametrize("external_change", [False, True])
def test_pending_typed_attempt_reconciles_without_receipt_or_action_replay(pending_typed, tmp_path, external_change):
    from worktree_controller import transaction_scope

    repository, worktree, _, store, journal, digest, options, alias = pending_typed
    original = journal.read_bytes()
    receipt_path = store.completion_receipts_dir / journal.name
    original_receipt = receipt_path.read_bytes() if receipt_path.exists() else None
    data = alias.read_bytes()
    if external_change:
        os.link(alias, tmp_path / "second-external-build")
    compatible = r.rollout_check(repository.repo)
    assert any(row["state"] == "TYPED_NO_ACTION_RECOVERABLE" for row in compatible["decision"]["transactions"])
    planned = a.reconcile_typed(repository.repo, journal.stem, digest, dry_run=True)["decision"]["plan"]
    result = a.reconcile_typed(repository.repo, journal.stem, digest, dry_run=False,
                               expected_plan_id=planned["plan_id"])
    assert result["decision"]["code"] == "TYPED_ATTEMPT_SUPERSEDED"
    with store.lock(exclusive=False):
        assert transaction_scope.terminal(store, journal.stem)
    fresh = lifecycle.cleanup_typed_disposable(repository.repo, **options, dry_run=True)["decision"]["plan"]
    done = lifecycle.cleanup_typed_disposable(repository.repo, **options, dry_run=False,
                                             expected_plan_id=fresh["plan_id"])
    assert done["decision"]["code"] == "TYPED_DISPOSABLE_CLEANED"
    assert done["decision"]["receipt_id"] != journal.stem
    lifecycle.release(repository.repo, target=worktree, expected_record_version=2,
                      owner_release="module-owner")
    assert journal.read_bytes() == original and alias.read_bytes() == data
    assert (receipt_path.read_bytes() if receipt_path.exists() else None) == original_receipt
    assert a.reconcile_typed(repository.repo, journal.stem, digest, dry_run=False)["decision"] == result["decision"]


@pytest.mark.parametrize("drift", ["started", "action-evidence", "receipt", "content", "missing", "ctime", "runtime"])
def test_pending_typed_no_action_recovery_rejects_uncertainty(pending_typed, monkeypatch, drift):
    repository, worktree, _, store, journal, digest, _, alias = pending_typed
    if drift == "started":
        value = json.loads(journal.read_text())
        value["actions"][0]["status"] = "STARTED"
        journal.write_text(json.dumps(value))
        _, digest = e.read(journal)
    elif drift == "action-evidence":
        e.write(store.root / "cleanup-actions" / journal.stem, "0000-before.json", {})
    elif drift == "receipt":
        # Corrupt this test-owned receipt, never a production recovery artifact.
        path = store.completion_receipts_dir / journal.name
        path.parent.mkdir(exist_ok=True)
        path.write_text("{}")
    elif drift == "content":
        alias.write_bytes(b"changed")
    elif drift == "missing":
        (worktree / "frontend/dist/assets/index.js").unlink()
    elif drift == "ctime":
        # An in-scope inode without external links has no churn that explains ctime.
        member = worktree / "frontend/dist/index.html"
        member.chmod(member.stat().st_mode)
    else:
        runtime = empty_runtime_evidence()
        runtime["managed_references"] = [{"pid": 999999}]
        monkeypatch.setattr(lifecycle, "runtime_authority_evidence", lambda *args, **kwargs: runtime)
    with pytest.raises(ControllerError):
        a.reconcile_typed(repository.repo, journal.stem, digest, dry_run=True)
    assert journal.exists() and not (store.root / "cleanup-recoveries" / journal.stem / "typed-no-action.json").exists()


@pytest.mark.parametrize("missing", [False, True])
def test_absent_completion_preserves_refusal_without_reading_none(
    interrupted, monkeypatch, missing
):
    repository = interrupted[0]
    ref = prepare(interrupted, preserve(interrupted), execute=True)["decision"]["recovery_receipt"]
    original = lifecycle.cleanup_typed_disposable

    def refused(repo, **options):
        result = original(repo, **{**options, "dry_run": True})
        if not options["dry_run"]:
            result["decision"].update(code="RESOURCE_CLEANUP_BLOCKED", receipt_id=None)
            if missing:
                del result["decision"]["receipt_id"]
            result["decision"]["plan"]["blockers"] = [
                {"code": "FIXTURE_REFUSAL", "message": "exact underlying refusal"}
            ]
        return result

    monkeypatch.setattr(lifecycle, "cleanup_typed_disposable", refused)
    result = r.resume(
        repository.repo, recovery_path=Path(ref["path"]), recovery_sha=ref["sha256"], dry_run=False
    )
    assert result["decision"]["code"] == "RECOVERY_ATTEMPT_INCOMPLETE"
    assert result["decision"]["underlying"]["blockers"][0]["code"] == "FIXTURE_REFUSAL"
    assert result["decision"]["phase"] == "ATTEMPT_FAILED_BEFORE_ACTION"
    evidence, _ = e.read(Path(result["decision"]["underlying_evidence"]["path"]))
    assert evidence["underlying"] == result["decision"]["underlying"]
    assert evidence["underlying"]["exit_code"] is None
    assert (
        evidence["underlying"]["stdout_summary"]["blockers"][0]["message"]
        == "exact underlying refusal"
    )


def legacy_started(fixture):
    new = preserve(fixture)
    ref = prepare(fixture, new, execute=True)["decision"]["recovery_receipt"]
    directory = Path(ref["path"]).parent
    started = e.write(
        directory,
        "started.json",
        {
            "contract": r.CONTRACT,
            "record_sha256": ref["sha256"],
            "timestamp": now_iso(),
        },
    )
    return ref, started, new


def reconcile(fixture, ref, *, execute=False):
    options = dict(recovery_path=Path(ref["path"]), recovery_sha=ref["sha256"])
    planned = r.reconcile_attempt(fixture[0].repo, **options, dry_run=True)
    if execute:
        return r.reconcile_attempt(
            fixture[0].repo,
            **options,
            dry_run=False,
            expected_plan_id=planned["decision"]["plan_id"],
        )
    return planned


def test_started_no_action_reconcile_supersede_resume_no_replay(interrupted, monkeypatch):
    repository, worktree, _, store, context, _, old_bytes, external = interrupted
    ref, started, new = legacy_started(interrupted)
    directory = Path(ref["path"]).parent
    old_bytes.update(
        {Path(item["path"]): Path(item["path"]).read_bytes() for item in (ref, started)}
    )
    with pytest.raises(ControllerError, match="explicit no-action reconciliation"):
        r.resume(
            repository.repo,
            recovery_path=Path(ref["path"]),
            recovery_sha=ref["sha256"],
            dry_run=False,
        )
    planned = reconcile(interrupted, ref)
    assert planned["decision"]["code"] == "STARTED_NO_ACTION_RECOVERABLE"
    assert not (directory / "reconciliations").exists()
    recorded = reconcile(interrupted, ref, execute=True)["decision"]["reconciliation"]
    reconciliation, _ = e.read(Path(recorded["path"]), recorded["sha256"])
    old_bytes[Path(recorded["path"])] = Path(recorded["path"]).read_bytes()
    assert reconciliation["historical_refusal"] == "NOT_RECONSTRUCTED"
    assert reconciliation["safe_to_supersede"] is True
    with pytest.raises((ControllerError, FileExistsError)):
        reconcile(interrupted, ref, execute=True)
    deleted = []
    original = lifecycle.delete_typed_resource

    def deletion(root, relative, evidence):
        deleted.append(relative)
        assert relative != r.COMPLETED_RESOURCE
        return original(root, relative, evidence)

    monkeypatch.setattr(lifecycle, "delete_typed_resource", deletion)
    result = r.resume(
        repository.repo, recovery_path=Path(ref["path"]), recovery_sha=ref["sha256"], dry_run=False
    )
    assert result["decision"]["code"] == "RECOVERY_CLEANED"
    assert result["decision"]["attempt_id"] != reconciliation["attempt"]["attempt_id"]
    assert deleted == r.REMAINING[:-1]
    assert len(a.chain(directory, ref["sha256"])) == 2
    assert r.completed(store, context["original_transaction_id"], verify_generations=True)
    assert all(path.read_bytes() == data for path, data in old_bytes.items())
    assert not any((worktree / name).exists() for name in r.REMAINING)
    assert external.read_bytes() == b"external intact"
    assert Path(new["receipt"]).is_file()


@pytest.mark.parametrize(
    "change",
    [
        "action",
        "completion",
        "filesystem",
        "topology",
        "preservation",
        "original-receipt",
        "recovery-record",
        "backend",
        "started",
    ],
)
def test_no_action_reconciliation_blocks_uncertain_or_changed_state(interrupted, change):
    _, worktree, _, store, context, _, _, _ = interrupted
    ref, started, new = legacy_started(interrupted)
    if change == "action":
        e.write(
            store.root / "cleanup-actions" / str(uuid.uuid4()),
            "0000-before.json",
            {"phase": "BEFORE"},
        )
    elif change == "completion":
        e.write(
            store.completion_receipts_dir,
            str(uuid.uuid4()) + ".json",
            {"plan": {"allocation_id": interrupted[2]}},
        )
    elif change == "filesystem":
        (worktree / "frontend/dist/assets/index.js").write_bytes(b"changed")
    elif change == "topology":
        file = worktree / "frontend/dist/assets/index.js"
        file.chmod(file.stat().st_mode)  # Same bytes/mode, unrecorded ctime mutation.
    elif change == "preservation":
        (Path(new["generation"]) / "tree/agent-workspace/opaque.sqlite").unlink()
    elif change in {"original-receipt", "recovery-record", "started"}:
        path = (
            store.completion_receipts_dir / (context["original_transaction_id"] + ".json")
            if change == "original-receipt"
            else Path((ref if change == "recovery-record" else started)["path"])
        )
        path.chmod(0o600)
        if change == "started":
            value = json.loads(path.read_text())
            value["record_sha256"] = "0" * 64
            path.write_text(json.dumps(value))
        else:
            path.write_bytes(path.read_bytes() + b" ")
    else:
        (worktree / r.COMPLETED_RESOURCE).mkdir()
    with pytest.raises((ControllerError, OSError)):
        reconcile(interrupted, ref, execute=True)
    assert not (Path(ref["path"]).parent / "reconciliations").exists()


def test_source_changes_after_reconciliation_blocks_new_attempt(interrupted):
    ref, _, _ = legacy_started(interrupted)
    reconcile(interrupted, ref, execute=True)
    (interrupted[1] / "frontend/dist/assets/index.js").write_bytes(b"changed after proof")
    with pytest.raises(ControllerError):
        r.resume(
            interrupted[0].repo,
            recovery_path=Path(ref["path"]),
            recovery_sha=ref["sha256"],
            dry_run=False,
        )
    assert not (Path(ref["path"]).parent / "attempts").exists()


def test_coordinator_diagnostic_exception_does_not_mask_refusal(interrupted, monkeypatch):
    repository = interrupted[0]
    ref = prepare(interrupted, preserve(interrupted), execute=True)["decision"]["recovery_receipt"]
    original_cleanup = lifecycle.cleanup_typed_disposable
    original_current = r._current
    calls = 0

    def current(*args):
        nonlocal calls
        calls += 1
        if calls > 1:
            raise RuntimeError("unsafe arbitrary exception text")
        return original_current(*args)

    def refused(repo, **options):
        result = original_cleanup(repo, **{**options, "dry_run": True})
        if not options["dry_run"]:
            result["decision"].update(code="RESOURCE_CLEANUP_BLOCKED", receipt_id=None)
            result["decision"]["plan"]["blockers"] = [{"code": "REAL_REFUSAL"}]
        return result

    monkeypatch.setattr(r, "_current", current)
    monkeypatch.setattr(lifecycle, "cleanup_typed_disposable", refused)
    result = r.resume(
        repository.repo, recovery_path=Path(ref["path"]), recovery_sha=ref["sha256"], dry_run=False
    )
    assert result["decision"]["underlying"]["blockers"] == [{"code": "REAL_REFUSAL"}]
    assert result["decision"]["no_action_verification_error"]["exception_type"] == "RuntimeError"
    assert "unsafe arbitrary exception text" not in json.dumps(result)


def test_underlying_exception_is_saved_before_reraise(interrupted, monkeypatch):
    ref = prepare(interrupted, preserve(interrupted), execute=True)["decision"]["recovery_receipt"]
    original = lifecycle.cleanup_typed_disposable

    def refused(repo, **options):
        if not options["dry_run"]:
            raise ControllerError("EXACT_REFUSAL", "fixture underlying error")
        return original(repo, **options)

    monkeypatch.setattr(lifecycle, "cleanup_typed_disposable", refused)
    with pytest.raises(ControllerError, match="fixture underlying error"):
        r.resume(
            interrupted[0].repo,
            recovery_path=Path(ref["path"]),
            recovery_sha=ref["sha256"],
            dry_run=False,
        )
    evidence = list(Path(ref["path"]).parent.glob("attempts/*/underlying.json"))
    value, _ = e.read(evidence[0])
    assert value["underlying"]["exception"]["code"] == "EXACT_REFUSAL"
    reconcile(interrupted, ref, execute=True)


@pytest.mark.parametrize(
    "kind", ["managed_references", "observed_references", "managed_visibility_uncertainties"]
)
def test_no_action_reconciliation_preserves_runtime_blockers(interrupted, monkeypatch, kind):
    ref, _, _ = legacy_started(interrupted)
    evidence = empty_runtime_evidence()
    evidence[kind] = [{"pid": 999999, "kind": "fixture", "reference": "fixture runtime"}]
    monkeypatch.setattr(lifecycle, "runtime_authority_evidence", lambda *args, **kwargs: evidence)
    with pytest.raises(ControllerError):
        reconcile(interrupted, ref, execute=True)


def test_unattributed_original_action_evidence_blocks(interrupted):
    ref, _, _ = legacy_started(interrupted)
    e.write(
        interrupted[3].root / "cleanup-actions" / interrupted[4]["original_transaction_id"],
        "0001-before.json",
        {"phase": "BEFORE", "resource": "frontend/dist"},
    )
    with pytest.raises(ControllerError, match="unattributed original action"):
        reconcile(interrupted, ref, execute=True)


def test_cli_incomplete_resume_returns_nonzero(monkeypatch, capsys, tmp_path):
    from worktree_controller import cli
    from worktree_controller.controller import envelope

    def incomplete(*args, **kwargs):
        value = envelope("resource-recover")
        value["decision"] = {
            "code": "RECOVERY_ATTEMPT_INCOMPLETE",
            "underlying": {"status": "REAL_REFUSAL"},
        }
        return value

    monkeypatch.setattr(r, "resume", incomplete)
    code = cli.main(
        [
            "resource-recover",
            "--repo",
            str(tmp_path),
            "--stage",
            "resume",
            "--recovery-receipt",
            str(tmp_path / "record.json"),
            "--recovery-receipt-sha256",
            "0" * 64,
            "--json",
        ]
    )
    assert code == 1
    assert json.loads(capsys.readouterr().out)["decision"]["underlying"]["status"] == "REAL_REFUSAL"


def test_result_write_exception_cannot_mask_underlying_refusal(interrupted, monkeypatch):
    ref = prepare(interrupted, preserve(interrupted), execute=True)["decision"]["recovery_receipt"]
    original = lifecycle.cleanup_typed_disposable
    write = e.write

    def refuse(repo, **options):
        result = original(repo, **{**options, "dry_run": True})
        if not options["dry_run"]:
            result["decision"].update(code="RESOURCE_CLEANUP_BLOCKED", receipt_id=None)
            result["decision"]["plan"]["blockers"] = [{"code": "PRIMARY_REFUSAL"}]
        return result

    def failed_write(directory, name, value):
        if name == "result.json":
            raise OSError(5, "fixture evidence write failed")
        return write(directory, name, value)

    monkeypatch.setattr(lifecycle, "cleanup_typed_disposable", refuse)
    monkeypatch.setattr(e, "write", failed_write)
    result = r.resume(
        interrupted[0].repo,
        recovery_path=Path(ref["path"]),
        recovery_sha=ref["sha256"],
        dry_run=False,
    )
    assert result["decision"]["underlying"]["blockers"] == [{"code": "PRIMARY_REFUSAL"}]
    assert result["decision"]["result_evidence_error"]["errno"] == 5
    saved, _ = e.read(Path(result["decision"]["underlying_evidence"]["path"]))
    assert saved["underlying"]["blockers"] == [{"code": "PRIMARY_REFUSAL"}]
