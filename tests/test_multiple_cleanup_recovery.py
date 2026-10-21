"""Interrupted multi cleanup is recovered through exact prefix/suffix evidence."""

from __future__ import annotations

import copy
import hashlib
import json
import uuid
from pathlib import Path

import pytest

from tests.test_controlled import _cli, acquire_one, adopt_canonical
from tests.test_retention_admission import POLICY_V5
from tests.test_shadow import POLICY, GitRepository, git
from tests.test_typed_disposable import empty_runtime_evidence
from worktree_controller import cleanup_evidence as e
from worktree_controller import cleanup_recovery, lifecycle
from worktree_controller import multiple_cleanup_recovery as r
from worktree_controller import preservation as p
from worktree_controller.cli import main
from worktree_controller.controller import doctor
from worktree_controller.delivered_closeout import archive_retained
from worktree_controller.git_backend import inventory
from worktree_controller.models import ControllerError
from worktree_controller.store import MetadataStore
from worktree_controller.transaction_scope import terminal_state


@pytest.fixture
def interrupted(tmp_path, monkeypatch, request):
    monkeypatch.setattr(lifecycle, "runtime_references", lambda *args, **kwargs: ())
    monkeypatch.setattr(lifecycle, "editable_references", lambda *args, **kwargs: ())
    monkeypatch.setattr(lifecycle, "runtime_authority_evidence", lambda *args, **kwargs: empty_runtime_evidence())
    monkeypatch.setattr(p, "evidence_root", lambda: tmp_path / "preservations")
    retained = getattr(request, "param", None) == "retained"
    crosslinked = getattr(request, "param", None) == "crosslinked"
    reopened = getattr(request, "param", None) in {"reopened", "reopened-terminal"}
    other_terminal = getattr(request, "param", None) in {"terminal", "reopened-terminal"}
    with GitRepository(linked=0, policy_text=POLICY_V5 if retained else POLICY) as repository:
        names = [f"state{step}" for step in range(6)]
        (repository.repo / ".gitignore").write_text("".join(f"{name}/\n" for name in names))
        git(repository.repo, "add", ".gitignore")
        git(repository.repo, "commit", "-m", "synthetic multiple ignored resources")
        adopt_canonical(repository)
        target, acquired = acquire_one(repository, role="feature" if retained else "hotfix")
        allocation = acquired["decision"]["allocation_id"]
        head = git(target, "rev-parse", "HEAD")
        if retained:
            (target / "unfinished.txt").write_text("unfinished fixture task\n")
            git(target, "add", "unfinished.txt")
            git(target, "commit", "-m", "synthetic retained task")
            lifecycle.park(repository.repo, target=target, expected_record_version=1, owner="module-owner")
        else:
            lifecycle.handoff(repository.repo, target=target, expected_record_version=1,
                kind="integration-ready", commit=head, finalizer_commit=head, finalizer_record_id="synthetic-fixture")
        if other_terminal:
            def stop_after_acquire_metadata(point):
                if point == "acquire_metadata_written":
                    raise ControllerError("FIXTURE_ACQUIRE_INTERRUPTION", "retain the official acquire journal")

            with monkeypatch.context() as patch:
                patch.setattr(lifecycle, "_crash_point", stop_after_acquire_metadata)
                with pytest.raises(ControllerError, match="retain the official acquire journal"):
                    acquire_one(repository, task="terminal-task", authority="terminal-authority")
            other = repository.root / "terminal-task"
            lifecycle.handoff(repository.repo, target=other, expected_record_version=1,
                kind="integration-ready", commit=head, finalizer_record_id="synthetic-terminal-fixture")
            lifecycle.release(repository.repo, target=other, expected_record_version=2,
                owner_release="module-owner")
            terminal_store = MetadataStore(inventory(repository.repo).common_dir)
            terminal_journal, = terminal_store.transactions_dir.glob("*.json")
            with terminal_store.lock(exclusive=False):
                assert terminal_state(terminal_store, terminal_journal.stem) == "ACQUIRE_RELEASE_SUPERSEDED"
        copied = []
        for name in names:
            (target / name).mkdir()
            (target / name / "opaque.bin").write_bytes((name + " fake bytes").encode())
        if crosslinked:
            (target / "state1/opaque.bin").unlink()
            (target / "state1/opaque.bin").hardlink_to(target / "state0/opaque.bin")
        for name in names:
            preserved = p.copy_preservation(p.plan_preservation(repository.repo, target, allocation,
                "module-owner", 2, source_relative=name))
            copied.append((Path(preserved["receipt"]), preserved["receipt_sha256"]))
        old_copied = []
        version = 2
        if reopened:
            old_copied = copied
            lifecycle.reopen(repository.repo, target=target, expected_record_version=2, owner="module-owner")
            (target / "rebuilt.txt").write_text("synthetic new source revision\n")
            git(target, "add", "rebuilt.txt")
            git(target, "commit", "-m", "synthetic rebuilt task revision")
            lifecycle.handoff(repository.repo, target=target, expected_record_version=3,
                kind="integration-ready", commit=git(target, "rev-parse", "HEAD"),
                finalizer_record_id="synthetic-reopened-finalizer", scope_paths=("rebuilt.txt",))
            git(repository.repo, "merge", "--ff-only", git(target, "branch", "--show-current"))
            version = 4
            copied = []
            for name in names:
                (target / name).rename(tmp_path / f"retired-{name}")
                (target / name).mkdir()
                (target / name / "opaque.bin").write_bytes((name + " rebuilt fake bytes").encode())
                preserved = p.copy_preservation(p.plan_preservation(repository.repo, target, allocation,
                    "module-owner", version, source_relative=name))
                copied.append((Path(preserved["receipt"]), preserved["receipt_sha256"]))
        options = dict(target=target, owner="module-owner", expected_record_version=version,
            preservation_receipt=copied[0][0], preservation_receipt_sha256=copied[0][1],
            additional_preservation_receipts=tuple(path for path, _ in copied[1:]),
            additional_preservation_receipt_sha256=tuple(digest for _, digest in copied[1:]))
        planned = lifecycle.cleanup_typed_disposable(repository.repo, **options, dry_run=True)["decision"]["plan"]
        assert not planned["blockers"] and planned["cleanup_paths"] == names
        original = lifecycle._typed_cleanup_plan_locked

        def interrupted_before_next_action(*args, **kwargs):
            if kwargs.get("cleanup_transaction_id") and len(kwargs.get("completed_action_evidence", ())) == 1:
                raise ControllerError("FIXTURE_PENDING_INTERRUPTION", "before the next action starts")
            return original(*args, **kwargs)

        with monkeypatch.context() as patch:
            patch.setattr(lifecycle, "_typed_cleanup_plan_locked", interrupted_before_next_action)
            with pytest.raises(ControllerError, match="immutable receipt"):
                lifecycle.cleanup_typed_disposable(repository.repo, **options, dry_run=False,
                    expected_plan_id=planned["plan_id"])
        store = MetadataStore(inventory(repository.repo).common_dir)
        journal_path, = (path for path in store.transactions_dir.glob("*.json")
                         if json.loads(path.read_bytes())["allocation_id"] == allocation)
        receipt_path = store.completion_receipts_dir / journal_path.name
        recovery = r.context(journal_path.stem, hashlib.sha256(journal_path.read_bytes()).hexdigest(),
                             hashlib.sha256(receipt_path.read_bytes()).hexdigest())
        old_paths = [*store.transactions_dir.glob("*.json"), receipt_path,
                     *list((store.root / "cleanup-actions").glob("**/*.json"))]
        old_paths.extend(path.parent / filename for path, _ in [*old_copied, *copied]
                         for filename in ("receipt.json", "plan.json", "manifest.json"))
        before = {path: path.read_bytes() for path in old_paths}
        yield repository, target, allocation, store, recovery, before


def prepare(state):
    repository, target, allocation, store, recovery, _ = state
    options = dict(target=target, owner="module-owner", version=store.load().records[allocation].record_version, recovery=recovery)
    planned = r.prepare(repository.repo, **options, dry_run=True)["decision"]
    assert len(planned["plan"]["preservation_evidences"]) == 6
    assert planned["plan"]["cleanup_paths"] == [f"state{step}" for step in range(1, 6)]
    return r.prepare(repository.repo, **options, dry_run=False,
                     expected_plan_id=planned["plan"]["plan_id"])["decision"]["recovery_receipt"]


def resume(state, ref, *, dry_run=False):
    return r.resume(state[0].repo, recovery_path=Path(ref["path"]), recovery_sha=ref["sha256"], dry_run=dry_run)


@pytest.mark.parametrize("interrupted", ["terminal", "reopened-terminal"], indirect=True)
def test_public_multi_recovery_with_another_released_terminal_journal(interrupted, capsys):
    repository, target, allocation, store, recovery, before = interrupted
    version = store.load().records[allocation].record_version
    other, = (path for path in store.transactions_dir.glob("*.json")
              if path.stem != recovery["original_transaction_id"])

    def invoke(*arguments):
        assert main([*arguments, "--repo", str(repository.repo), "--json"]) == 0
        return json.loads(capsys.readouterr().out)["decision"]

    arguments = ["resource-recover", "--stage", "prepare-multiple", "--path", str(target),
        "--owner", "module-owner", "--expected-record-version", str(version),
        "--transaction-id", recovery["original_transaction_id"],
        "--transaction-sha256", recovery["original_transaction_sha256"],
        "--cleanup-receipt-sha256", recovery["original_cleanup_receipt_sha256"]]
    planned = invoke(*arguments, "--dry-run")["plan"]
    assert len(planned["preservation_evidences"]) == 6
    assert planned["cleanup_paths"] == [f"state{step}" for step in range(1, 6)]
    assert all(path.read_bytes() == raw for path, raw in before.items())
    ref = invoke(*arguments, "--expected-plan-id", planned["plan_id"])["recovery_receipt"]
    resume_arguments = ["resource-recover", "--stage", "resume", "--recovery-receipt", ref["path"],
        "--recovery-receipt-sha256", ref["sha256"]]
    invoke(*resume_arguments, "--dry-run")
    assert invoke(*resume_arguments)["code"] == "MULTIPLE_RECOVERY_CLEANED"
    release_arguments = ["release", "--path", str(target), "--owner-release", "module-owner",
        "--expected-record-version", str(version)]
    invoke(*release_arguments, "--dry-run")
    invoke(*release_arguments)
    assert not target.exists()
    assert all(path.read_bytes() == raw for path, raw in before.items())
    with store.lock(exclusive=False):
        assert terminal_state(store, other.stem, verify_generations=True) == "ACQUIRE_RELEASE_SUPERSEDED"
        assert terminal_state(store, recovery["original_transaction_id"], verify_generations=True) == "RECOVERY_CLEANED"
    assert cleanup_recovery.rollout_check(repository.repo)["decision"]["code"] == "ROLLOUT_RECOVERY_COMPATIBLE"


@pytest.mark.parametrize("interrupted", ["terminal"], indirect=True)
def test_other_terminal_journal_still_requires_the_actual_store_lock(interrupted):
    repository, target, _, store, recovery, _ = interrupted
    with pytest.raises(ControllerError) as blocked:
        r.original(store, recovery, target=target)
    assert blocked.value.code == "MISSING_REPOSITORY_LOCK"
    with store.lock(exclusive=False):
        unheld = MetadataStore(inventory(repository.repo).common_dir)
        with pytest.raises(ControllerError) as blocked:
            r.original(unheld, recovery, target=target)
        assert blocked.value.code == "MISSING_REPOSITORY_LOCK"
        assert r.original(store, recovery, target=target)


@pytest.mark.parametrize("interrupted", ["terminal"], indirect=True)
def test_another_unfinished_journal_still_blocks_multi_recovery(interrupted):
    repository, target, _, store, recovery, before = interrupted
    transaction = str(uuid.uuid4())
    e.write(store.transactions_dir, f"{transaction}.json", {
        "transaction_id": transaction, "command": "unknown", "phase": "PREPARED"})
    with pytest.raises(ControllerError, match="another interrupted transaction"):
        r.prepare(repository.repo, target=target, owner="module-owner", version=2,
            recovery=recovery, dry_run=True)
    assert not (store.root / "cleanup-recoveries").exists()
    assert all(path.read_bytes() == raw for path, raw in before.items())
    assert all((target / f"state{step}").exists() for step in range(1, 6))


def test_six_proofs_resume_only_pending_suffix_and_close_original(interrupted, monkeypatch):
    repository, target, _, store, recovery, before = interrupted
    ref = prepare(interrupted)
    original_delete = lifecycle.delete_preserved_state
    deleted = []

    def guarded(root, evidence):
        relative = evidence["allocation_source_identity"]["target_path"]
        _, source_plan = r.coverage(Path(evidence["receipt_path"]), evidence["receipt_sha256"], verify_source=False)
        name = Path(source_plan["source"]).relative_to(Path(relative)).as_posix()
        assert name != "state0"
        deleted.append(name)
        return original_delete(root, evidence)

    monkeypatch.setattr(lifecycle, "delete_preserved_state", guarded)
    result = resume(interrupted, ref)
    assert result["decision"]["code"] == "MULTIPLE_RECOVERY_CLEANED"
    assert deleted == [f"state{step}" for step in range(1, 6)]
    assert all(path.read_bytes() == raw for path, raw in before.items())
    assert not any((target / f"state{step}").exists() for step in range(6))
    with store.lock(exclusive=False):
        assert r.completed(store, recovery["original_transaction_id"], verify_generations=True)
        assert terminal_state(store, recovery["original_transaction_id"], verify_generations=True) == "RECOVERY_CLEANED"
        assert len(r.verified_completion(store, recovery["original_transaction_id"])) == 6
    assert not any(warning["code"] == "INTERRUPTED_TRANSACTION" for warning in doctor(repository.repo)["warnings"])
    again = resume(interrupted, ref)
    assert again["decision"]["receipt_id"] == result["decision"]["receipt_id"]
    assert deleted == [f"state{step}" for step in range(1, 6)]
    lifecycle.release(repository.repo, target=target, expected_record_version=2, owner_release="module-owner")
    assert not target.exists()
    with store.lock(exclusive=False):
        assert r.completed(store, recovery["original_transaction_id"], verify_generations=True)
    assert not any(warning["code"] == "INTERRUPTED_TRANSACTION" for warning in doctor(repository.repo)["warnings"])


@pytest.mark.parametrize("mutation", ["before", "after", "extra-before", "source-reappeared", "generation", "receipt", "journal"])
def test_prefix_or_proof_mutation_blocks_before_new_record(interrupted, mutation):
    repository, target, _, store, recovery, before = interrupted
    transaction = recovery["original_transaction_id"]
    if mutation == "source-reappeared":
        (target / "state0").mkdir()
    elif mutation == "extra-before":
        e.write(store.root / "cleanup-actions" / transaction, "0001-before.json", {"phase": "BEFORE"})
    else:
        if mutation in {"before", "after"}:
            path = store.root / "cleanup-actions" / transaction / f"0000-{mutation}.json"
        elif mutation == "generation":
            path = next(path for path in before if path.name == "manifest.json")
        else:
            parent = store.transactions_dir if mutation == "journal" else store.completion_receipts_dir
            path = parent / f"{transaction}.json"
        path.chmod(0o600)
        path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises(ControllerError):
        r.prepare(repository.repo, target=target, owner="module-owner", version=2, recovery=recovery, dry_run=True)
    assert not (store.root / "cleanup-recoveries" / transaction).exists()
    assert all((target / f"state{step}").exists() for step in range(1, 6))


@pytest.mark.parametrize("failure_step", [1, 2])
def test_second_interruption_keeps_both_journals_and_refuses_replay(interrupted, monkeypatch, failure_step):
    _, target, _, store, _, before = interrupted
    ref = prepare(interrupted)

    original_delete = lifecycle.delete_preserved_state
    count = 0

    def failed_delete(*args, **kwargs):
        nonlocal count
        count += 1
        if count == failure_step:
            raise ControllerError("FIXTURE_SECOND_INTERRUPTION", "new action has started")
        return original_delete(*args, **kwargs)

    monkeypatch.setattr(lifecycle, "delete_preserved_state", failed_delete)
    with pytest.raises(ControllerError):
        resume(interrupted, ref)
    assert len(list(store.transactions_dir.glob("*.json"))) == 2
    assert not (Path(ref["path"]).parent / "complete.json").exists()
    assert all(path.read_bytes() == raw for path, raw in before.items())
    with pytest.raises(ControllerError):
        resume(interrupted, ref)
    with pytest.raises(ControllerError):
        cleanup_recovery.rollout_check(interrupted[0].repo)
    assert all((target / f"state{step}").exists() for step in range(failure_step, 6))
    assert count == failure_step


def test_rollout_reads_supported_prefix_without_mutation(interrupted):
    repository, _, _, store, _, before = interrupted
    checked = cleanup_recovery.rollout_check(repository.repo)
    assert checked["decision"]["transactions"][0]["state"] == "MULTIPLE_PREFIX_RECOVERABLE"
    assert not (store.root / "cleanup-recoveries").exists()
    assert all(path.read_bytes() == raw for path, raw in before.items())


def test_started_coordinator_without_action_still_refuses_rollout_and_replay(interrupted):
    ref = prepare(interrupted)
    record, _ = e.read(Path(ref["path"]), ref["sha256"])
    e.write(Path(ref["path"]).parent, "started.json", {"contract": r.CONTRACT,
        "record_sha256": ref["sha256"], "recovery_id": record["recovery_id"], "phase": "ATTEMPT_STARTED"})
    with pytest.raises(ControllerError):
        cleanup_recovery.rollout_check(interrupted[0].repo)
    with pytest.raises(ControllerError, match="started recovery cannot replay"):
        resume(interrupted, ref)


def test_terminal_coordinator_interruption_appends_without_execution(interrupted, monkeypatch):
    ref = prepare(interrupted)
    original_append = r._append_terminal

    def failed_append(*args, **kwargs):
        raise ControllerError("FIXTURE_TERMINAL_INTERRUPTION", "suffix receipt already exists")

    monkeypatch.setattr(r, "_append_terminal", failed_append)
    with pytest.raises(ControllerError, match="suffix receipt already exists"):
        resume(interrupted, ref)
    monkeypatch.setattr(r, "_append_terminal", original_append)
    monkeypatch.setattr(lifecycle, "delete_preserved_state", lambda *args, **kwargs: pytest.fail("suffix replayed"))
    assert resume(interrupted, ref)["decision"]["code"] == "MULTIPLE_RECOVERY_CLEANED"


@pytest.mark.parametrize("mutation", ["proofset", "prefix-topology", "terminal", "suffix-after"])
def test_terminal_reverification_rejects_changed_binding(interrupted, mutation):
    _, _, _, store, recovery, _ = interrupted
    ref = prepare(interrupted)
    result = resume(interrupted, ref)
    receipt_id = result["decision"]["receipt_id"]
    terminal_path = Path(result["decision"]["recovery_completion"]["path"])
    if mutation == "suffix-after":
        path = store.root / "cleanup-actions" / receipt_id / "0000-after.json"
        path.chmod(0o600)
        path.write_bytes(path.read_bytes() + b" ")
    elif mutation == "terminal":
        value = json.loads(terminal_path.read_bytes())
        value["completion_receipt_id"] = recovery["original_transaction_id"]
        terminal_path.chmod(0o600)
        terminal_path.write_text(json.dumps(value))
    else:
        path = store.completion_receipts_dir / f"{receipt_id}.json"
        value = copy.deepcopy(json.loads(path.read_bytes()))
        if mutation == "proofset":
            value["plan"]["preservation_evidences"].pop()
        else:
            value["plan"]["hardlink_topology"] = value["post_scan"]["hardlink_topology"]
        path.chmod(0o600)
        path.write_text(json.dumps(value))
    with store.lock(exclusive=False), pytest.raises(ControllerError):
        r.completed(store, recovery["original_transaction_id"], verify_generations=True)


@pytest.mark.parametrize("interrupted", ["crosslinked"], indirect=True)
def test_prefix_hardlink_effect_becomes_suffix_exact_baseline(interrupted):
    _, target, _, store, recovery, _ = interrupted
    _, _, completed, topology = r.details(store, recovery)
    assert completed == {"state0"}
    assert topology["resources"] == [f"state{step}" for step in range(1, 6)]
    assert (target / "state1/opaque.bin").stat().st_nlink == 1
    ref = prepare(interrupted)
    assert resume(interrupted, ref)["decision"]["code"] == "MULTIPLE_RECOVERY_CLEANED"


@pytest.mark.parametrize("interrupted", ["retained"], indirect=True)
def test_archived_release_keeps_terminal_and_rollout_valid(interrupted):
    repository, target, _, store, recovery, _ = interrupted
    ref = prepare(interrupted)
    resume(interrupted, ref)
    options = dict(target=target, expected_record_version=2, owner_release="module-owner", actor="governance")
    archived = archive_retained(repository.repo, **options, dry_run=True)
    archive_retained(repository.repo, **options, plan_id=archived["decision"]["plan_id"])
    lifecycle.release(repository.repo, target=target, expected_record_version=3, owner_release="module-owner")
    with store.lock(exclusive=False):
        assert terminal_state(store, recovery["original_transaction_id"], verify_generations=True) == "RECOVERY_CLEANED"
    assert cleanup_recovery.rollout_check(repository.repo)["decision"]["transactions"][0]["state"] == "RECOVERY_CLEANED"


@pytest.mark.parametrize("mutation", ["released-head", "release-cas", "release-plan", "missing-release"])
def test_absent_checkout_requires_exact_official_release(interrupted, mutation):
    repository, target, _, store, recovery, _ = interrupted
    ref = prepare(interrupted)
    resume(interrupted, ref)
    lifecycle.release(repository.repo, target=target, expected_record_version=2, owner_release="module-owner")
    path, = store.release_receipts_dir.glob("*.json")
    if mutation == "missing-release":
        path.unlink()
    else:
        value = json.loads(path.read_bytes())
        if mutation == "released-head":
            value["released_head"] = "0" * 40
        elif mutation == "release-cas":
            value["expected_record_version"] += 1
        else:
            path = store.release_plans_dir / f"{value['plan_id']}.json"
            value = json.loads(path.read_bytes())
            value["expected_head"] = "0" * 40
        path.chmod(0o600)
        path.write_text(json.dumps(value))
    with store.lock(exclusive=False), pytest.raises(ControllerError):
        r.completed(store, recovery["original_transaction_id"], verify_generations=True)


def test_invalid_cli_transaction_is_structured_refusal(interrupted):
    repository, target, _, _, recovery, _ = interrupted
    result = _cli(["resource-recover", "--repo", str(repository.repo), "--stage", "prepare-multiple",
        "--path", str(target), "--owner", "module-owner", "--expected-record-version", "2",
        "--transaction-id", "not-a-uuid", "--transaction-sha256", recovery["original_transaction_sha256"],
        "--cleanup-receipt-sha256", recovery["original_cleanup_receipt_sha256"], "--dry-run", "--json"])
    assert result.returncode != 0
    assert "Traceback" not in result.stderr
    assert json.loads(result.stdout)["errors"][0]["code"] == "MULTIPLE_CLEANUP_RECOVERY_BLOCKED"


@pytest.mark.parametrize("mutation", ["started-suffix", "missing-proof", "reduced-proofset", "content-drift"])
def test_unsafe_shape_or_changed_suffix_remains_blocked_with_fresh_raw_hashes(interrupted, mutation):
    repository, target, _, store, recovery, _ = interrupted
    journal_path = store.transactions_dir / f"{recovery['original_transaction_id']}.json"
    receipt_path = store.completion_receipts_dir / journal_path.name
    journal = json.loads(journal_path.read_bytes())
    receipt = json.loads(receipt_path.read_bytes())
    if mutation == "content-drift":
        (target / "state1/opaque.bin").write_bytes(b"changed fake bytes")
    elif mutation == "started-suffix":
        journal["actions"][1]["status"] = receipt["actions"][1]["status"] = "STARTED"
    elif mutation == "missing-proof":
        receipt["action_evidence"] = []
    else:
        receipt["plan"]["preservation_evidences"].pop()
        receipt["plan"]["plan_id"] = lifecycle._release_plan_id({key: value for key, value in receipt["plan"].items() if key != "plan_id"})
        journal["cleanup_plan"] = receipt["plan"]
    for path, value in ((journal_path, journal), (receipt_path, receipt)):
        path.chmod(0o600)
        path.write_text(json.dumps(value))
    current = r.context(journal_path.stem, hashlib.sha256(journal_path.read_bytes()).hexdigest(),
                        hashlib.sha256(receipt_path.read_bytes()).hexdigest())
    with pytest.raises(ControllerError):
        r.prepare(repository.repo, target=target, owner="module-owner", version=2, recovery=current, dry_run=True)


def test_cli_malformed_record_with_exact_raw_hash_is_structured_refusal(interrupted):
    ref = prepare(interrupted)
    path = Path(ref["path"])
    path.chmod(0o600)
    path.write_text(json.dumps({"contract": r.CONTRACT}))
    result = _cli(["resource-recover", "--repo", str(interrupted[0].repo), "--stage", "resume",
        "--recovery-receipt", str(path), "--recovery-receipt-sha256", hashlib.sha256(path.read_bytes()).hexdigest(),
        "--dry-run", "--json"])
    assert result.returncode != 0 and "Traceback" not in result.stderr
    assert json.loads(result.stdout)["errors"][0]["code"] == "MULTIPLE_CLEANUP_RECOVERY_BLOCKED"


def test_terminal_append_refuses_unknown_self_bound_successor(interrupted, monkeypatch):
    ref = prepare(interrupted)
    original_append = r._append_terminal

    def failed_append(*args, **kwargs):
        raise ControllerError("FIXTURE_TERMINAL_INTERRUPTION", "suffix receipt exists")

    monkeypatch.setattr(r, "_append_terminal", failed_append)
    with pytest.raises(ControllerError):
        resume(interrupted, ref)
    monkeypatch.setattr(r, "_append_terminal", original_append)
    _, _, _, store, recovery, _ = interrupted
    successful = next(e.read(path)[0] for path in store.completion_receipts_dir.glob("*.json")
                      if e.read(path)[0].get("result") == "CLEANED")
    unknown_id = str(uuid.uuid4())
    journal = store._read_json(store.transactions_dir / f"{recovery['original_transaction_id']}.json")
    journal["transaction_id"] = unknown_id
    journal["cleanup_plan"] = successful["plan"]
    journal["actions"] = [{"path": name, "status": "PENDING"} for name in successful["plan"]["cleanup_paths"]]
    e.write(store.transactions_dir, f"{unknown_id}.json", journal)
    with pytest.raises(ControllerError, match="another interrupted transaction"):
        resume(interrupted, ref)
    assert not (Path(ref["path"]).parent / "complete.json").exists()


@pytest.mark.parametrize("interrupted", ["reopened"], indirect=True)
def test_reopened_twelve_generations_multi_resume_release_preserves_history(interrupted):
    repository, target, _, store, recovery, before = interrupted
    assert len([path for path in before if path.name == "receipt.json"]) == 12
    ref = prepare(interrupted)
    result = resume(interrupted, ref)
    assert result["decision"]["code"] == "MULTIPLE_RECOVERY_CLEANED"
    assert all(path.read_bytes() == raw for path, raw in before.items())
    lifecycle.release(repository.repo, target=target, expected_record_version=4, owner_release="module-owner")
    with store.lock(exclusive=False):
        assert terminal_state(store, recovery["original_transaction_id"], verify_generations=True) == "RECOVERY_CLEANED"
    assert all(path.read_bytes() == raw for path, raw in before.items())


@pytest.mark.parametrize("interrupted", ["reopened"], indirect=True)
@pytest.mark.parametrize("mutation", [
    "old-plan", "old-receipt", "old-manifest", "missing-generation", "new-generation",
    "missing-plan", "missing-receipt", "missing-manifest", "new-manifest", "replaced-generation",
])
def test_released_combined_recovery_freezes_all_generation_metadata(interrupted, mutation, tmp_path):
    repository, target, _, store, recovery, before = interrupted
    old_plan_path = next(path for path in before if path.name == "plan.json"
                         and json.loads(path.read_bytes())["authority"]["identity"]["expected_record_version"] == 2)
    generation = old_plan_path.parent
    manifest_bytes = (generation / "manifest.json").read_bytes()
    if mutation == "new-manifest":
        generation.chmod(0o700)
        (generation / "manifest.json").unlink()
    ref = prepare(interrupted)
    record, _ = e.read(Path(ref["path"]), ref["sha256"])
    assert record["allocation_record"]["record_version"] == 4
    assert len(record["generation_metadata"]) == 12
    resume(interrupted, ref)
    lifecycle.release(repository.repo, target=target, expected_record_version=4, owner_release="module-owner")
    if mutation in {"missing-generation", "new-generation", "replaced-generation"}:
        generation.parent.chmod(0o700)
        generation.chmod(0o700)
    if mutation.startswith("old-"):
        name = {"old-plan": "plan.json", "old-receipt": "receipt.json", "old-manifest": "manifest.json"}[mutation]
        path = generation / name
        path.chmod(0o600)
        path.write_bytes(path.read_bytes() + b" ")
    elif mutation.startswith("missing-"):
        if mutation == "missing-generation":
            generation.rename(tmp_path / "removed-generation")
        else:
            generation.chmod(0o700)
            name = {"missing-plan": "plan.json", "missing-receipt": "receipt.json", "missing-manifest": "manifest.json"}[mutation]
            (generation / name).unlink()
    elif mutation == "new-generation":
        added = generation.parent / str(uuid.uuid4())
        for name in ("plan.json", "manifest.json"):
            e.write(added, name, json.loads((generation / name).read_bytes()))
        copied = json.loads((generation / "receipt.json").read_bytes())
        with p._absolute_directory(added) as chain:
            copied.update(receipt_id=added.name, generation=str(added), generation_identity=chain[-1].identity)
        e.write(added, "receipt.json", copied)
    elif mutation == "replaced-generation":
        original = {name: (generation / name).read_bytes() for name in ("plan.json", "receipt.json", "manifest.json")}
        generation.rename(tmp_path / "retired-generation")
        generation.mkdir()
        for name, raw in original.items():
            (generation / name).write_bytes(raw)
    else:
        generation.chmod(0o700)
        (generation / "manifest.json").write_bytes(manifest_bytes)
    with store.lock(exclusive=False), pytest.raises(ControllerError):
        r.completed(store, recovery["original_transaction_id"], verify_generations=True)
    with pytest.raises(ControllerError):
        cleanup_recovery.rollout_check(repository.repo)


def test_unreceipted_partial_generation_keeps_existing_no_authority_rule(interrupted):
    repository, target, _, store, recovery, _ = interrupted
    ref = prepare(interrupted)
    resume(interrupted, ref)
    lifecycle.release(repository.repo, target=target, expected_record_version=2, owner_release="module-owner")
    old, _ = e.read(store.completion_receipts_dir / f"{recovery['original_transaction_id']}.json")
    source = Path(old["plan"]["preservation_evidence"]["receipt_path"]).parent
    added = source.parent / str(uuid.uuid4())
    e.write(added, "plan.json", {"incomplete": True})
    with store.lock(exclusive=False):
        assert r.completed(store, recovery["original_transaction_id"], verify_generations=True)


@pytest.mark.parametrize("mutation", ["history", "authority-key", "base-commit", "scope-paths"])
def test_released_record_preserves_frozen_authority_and_history(interrupted, mutation):
    repository, target, allocation, store, recovery, _ = interrupted
    ref = prepare(interrupted)
    resume(interrupted, ref)
    lifecycle.release(repository.repo, target=target, expected_record_version=2, owner_release="module-owner")
    path = store.records_dir / f"{allocation}.json"
    current = json.loads(path.read_bytes())
    if mutation == "history":
        current["history"][0]["owner"] = "another-fixture-owner"
    elif mutation == "authority-key":
        current["authority_key"] = "changed-fixture-authority"
    elif mutation == "base-commit":
        current["base_commit"] = "0" * 40
    else:
        current["scope_paths"] = ["changed-fixture-scope.py"]
    path.write_text(json.dumps(current))
    with store.lock(exclusive=False), pytest.raises(ControllerError):
        r.completed(store, recovery["original_transaction_id"], verify_generations=True)


def test_live_recovery_snapshot_history_must_match_actual_record(interrupted, monkeypatch):
    ref = prepare(interrupted)
    path = Path(ref["path"])
    record, _ = e.read(path, ref["sha256"])
    record["allocation_record"]["history"][0]["owner"] = "different-fixture-owner"
    path.chmod(0o600)
    path.write_text(json.dumps(record))
    changed = {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    monkeypatch.setattr(lifecycle, "delete_preserved_state", lambda *args, **kwargs: pytest.fail("action started"))
    with pytest.raises(ControllerError, match="live allocation record differs"):
        resume(interrupted, changed)
