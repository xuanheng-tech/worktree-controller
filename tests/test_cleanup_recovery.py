from __future__ import annotations

import copy
import hashlib
import json
import os
import uuid
from pathlib import Path

import pytest

from tests.test_controlled import (
    _cli,
    _release_arguments,
    acquire_one,
    adopt_canonical,
    handoff_one,
)
from tests.test_preserved_cleanup import clean, dry_run
from tests.test_preserved_cleanup import preserved as preserved
from tests.test_shadow import GitRepository, git
from tests.test_typed_disposable import target as target
from worktree_controller import cleanup_evidence as e
from worktree_controller import cleanup_recovery as r
from worktree_controller import disposable, hardlinks, lifecycle
from worktree_controller import preservation as p
from worktree_controller.cli import main
from worktree_controller.controller import doctor
from worktree_controller.git_backend import inventory
from worktree_controller.models import ControllerError
from worktree_controller.store import MetadataStore, now_iso


def metadata_bytes(store):
    return {path.relative_to(store.root): path.read_bytes()
            for path in store.root.rglob("*") if path.is_file()}


def rollout_error(repository, capsys, *options):
    assert main([
        "resource-recover", "--repo", str(repository.repo), "--stage", "rollout-check",
        "--dry-run", "--json", *options,
    ]) == 2
    payload = json.loads(capsys.readouterr().out)
    diagnostic, = payload["decision"]["transaction_diagnostics"]
    assert payload["errors"][0]["code"] == diagnostic["error"]["code"]
    return diagnostic


@pytest.mark.parametrize("repo_entry", ["canonical", "target", "target-subdirectory"])
def test_cli_release_finishes_when_repo_entry_is_removed(repo_entry):
    with GitRepository(linked=1) as repository:
        repository.adopt_all()
        worktree, acquired = acquire_one(repository)
        handoff_one(repository, worktree)
        store = MetadataStore(inventory(repository.repo).common_dir)
        peer = repository.linked[0]
        peer_policy = (peer / ".agents/worktree-policy.toml").read_bytes()
        before = store.load()
        refs = git(repository.repo, "show-ref")
        entries = {
            "canonical": repository.repo,
            "target": worktree,
            "target-subdirectory": worktree / ".agents",
        }
        arguments = _release_arguments(repository, worktree)
        arguments[arguments.index("--repo") + 1] = str(entries[repo_entry])

        result = _cli(arguments)
        payload = json.loads(result.stdout)
        assert result.returncode == 0, payload
        assert payload["decision"]["code"] == "RELEASED"
        assert not worktree.exists()
        assert (peer / ".agents/worktree-policy.toml").read_bytes() == peer_policy
        assert git(repository.repo, "show-ref") == refs
        assert not list(store.transactions_dir.glob("*.json"))
        allocation = str(acquired["decision"]["allocation_id"])
        after = store.load()
        assert after.records[allocation].lifecycle == "RELEASED"
        assert {
            key: record for key, record in after.records.items() if key != allocation
        } == {
            key: record for key, record in before.records.items() if key != allocation
        }
        receipt_id = payload["decision"]["release_receipt_id"]
        assert after.release_receipts[receipt_id].allocation_id == allocation
        history = _cli([
            "lifecycle-history", "--repo", str(repository.repo),
            "--allocation-id", allocation, "--json",
        ])
        assert history.returncode == 0, history.stdout
        decision = json.loads(history.stdout)["decision"]
        closed, = decision["closed_releases"]
        assert closed["proof_status"] == "COMPLETE"
        assert closed["release_receipt_id"] == receipt_id
        assert decision["incomplete_history"] == []

        completed = metadata_bytes(store)
        replay = _cli(_release_arguments(repository, worktree))
        assert replay.returncode == 0, replay.stdout
        assert json.loads(replay.stdout)["decision"]["code"] == "ALREADY_RELEASED"
        assert json.loads(replay.stdout)["decision"]["release_receipt_id"] == receipt_id
        assert metadata_bytes(store) == completed
        assert git(repository.repo, "show-ref") == refs


def test_cli_recovers_recorded_release_and_replays_without_mutation():
    with GitRepository(linked=0) as repository:
        adopt_canonical(repository)
        worktree, acquired = acquire_one(repository)
        handoff_one(repository, worktree)
        branch = "refs/heads/feature/new-task"
        head = git(repository.repo, "rev-parse", branch)
        crashed = _cli(_release_arguments(repository, worktree), crash_at="release_metadata_written")
        assert crashed.returncode == 97
        store = MetadataStore(inventory(repository.repo).common_dir)
        path, = store.transactions_dir.glob("*.json")
        journal, _ = e.read(path)
        assert journal["phase"] == "RELEASED_RECORDED"
        assert not worktree.exists()
        assert not (store.release_receipts_dir / path.name).exists()
        allocation = acquired["decision"]["allocation_id"]
        released_record = store.load().records[allocation]
        assert released_record.lifecycle == "RELEASED"
        plan_ids = set(store.load().release_plans)
        before = metadata_bytes(store)
        rollout_arguments = [
            "resource-recover", "--repo", str(repository.repo), "--stage", "rollout-check",
            "--dry-run", "--json",
        ]
        refused = _cli(rollout_arguments)
        assert refused.returncode == 2, refused.stderr
        diagnostic, = json.loads(refused.stdout)["decision"]["transaction_diagnostics"]
        assert diagnostic == {
            "transaction_id": path.stem, "command": "release", "phase": "RELEASED_RECORDED",
            "error": {
                "code": "ROLLOUT_RELEASE_RECOVERY_REQUIRED",
                "message": "unfinished release requires exact replay through release with the original request identity",
            },
            "recovery_entry": "release",
        }
        assert metadata_bytes(store) == before
        assert git(repository.repo, "rev-parse", branch) == head
        assert not worktree.exists()
        dry_run = _cli(_release_arguments(repository, worktree, dry_run=True))
        assert dry_run.returncode == 2, dry_run.stderr
        assert json.loads(dry_run.stdout)["errors"][0]["code"] == "INTERRUPTED_TRANSACTION"
        assert metadata_bytes(store) == before
        recovery = _cli(_release_arguments(repository, worktree))
        assert recovery.returncode == 0, recovery.stderr
        recovered = json.loads(recovery.stdout)
        assert recovered["decision"]["code"] == "RELEASE_RECOVERED"
        assert recovered["decision"]["release_receipt_id"] == path.stem
        assert not path.exists()
        assert (store.release_receipts_dir / path.name).is_file()
        assert store.load().records[allocation] == released_record
        assert set(store.load().release_plans) == plan_ids
        completed = metadata_bytes(store)
        replay = _cli(_release_arguments(repository, worktree))
        assert replay.returncode == 0, replay.stderr
        replayed = json.loads(replay.stdout)
        assert replayed["decision"]["code"] == "ALREADY_RELEASED"
        assert replayed["decision"]["release_receipt_id"] == path.stem
        compatible = _cli(rollout_arguments)
        assert compatible.returncode == 0, compatible.stderr
        assert json.loads(compatible.stdout)["decision"]["code"] == "ROLLOUT_RECOVERY_COMPATIBLE"
        assert metadata_bytes(store) == completed
        assert git(repository.repo, "rev-parse", branch) == head
        assert not worktree.exists()


@pytest.mark.parametrize(("drift", "error_code"), [
    ("owner", "RELEASE_RECOVERY_MISMATCH"),
    ("record_version", "RELEASE_RECOVERY_MISMATCH"),
    ("retained_ref", "MISSING_RETAINED_REF"),
])
def test_cli_recorded_release_recovery_rejects_identity_drift_without_mutation(drift, error_code):
    with GitRepository(linked=0) as repository:
        adopt_canonical(repository)
        worktree, _ = acquire_one(repository)
        handoff_one(repository, worktree)
        arguments = _release_arguments(repository, worktree)
        crashed = _cli(arguments, crash_at="release_metadata_written")
        assert crashed.returncode == 97
        store = MetadataStore(inventory(repository.repo).common_dir)
        transaction, = store.transactions_dir.glob("*.json")
        journal, _ = e.read(transaction)
        assert journal["phase"] == "RELEASED_RECORDED"
        assert not worktree.exists()
        assert not (store.release_receipts_dir / transaction.name).exists()
        branch = "refs/heads/feature/new-task"
        if drift == "owner":
            arguments[arguments.index("--owner-release") + 1] = "other-owner"
        elif drift == "record_version":
            arguments[arguments.index("--expected-record-version") + 1] = "3"
        else:
            git(repository.repo, "commit", "--allow-empty", "-m", "advance fixture main")
            git(repository.repo, "branch", "-f", "feature/new-task", "HEAD")
        head = git(repository.repo, "rev-parse", branch)
        before = metadata_bytes(store)
        refused = _cli(arguments)
        assert refused.returncode == 2, refused.stderr
        assert json.loads(refused.stdout)["errors"][0]["code"] == error_code
        blocked = _cli([
            "resource-recover", "--repo", str(repository.repo), "--stage", "rollout-check",
            "--dry-run", "--json",
        ])
        assert blocked.returncode == 2, blocked.stderr
        diagnostic, = json.loads(blocked.stdout)["decision"]["transaction_diagnostics"]
        assert diagnostic["transaction_id"] == transaction.stem
        assert diagnostic["phase"] == "RELEASED_RECORDED"
        assert diagnostic["error"]["code"] == "ROLLOUT_RELEASE_RECOVERY_REQUIRED"
        assert metadata_bytes(store) == before
        assert not worktree.exists()
        assert git(repository.repo, "rev-parse", branch) == head


@pytest.mark.parametrize("journal", [
    {},
    {"command": "unrecognized", "phase": "PREPARED",
     "cleanup_plan": {"contract": "legacy-preserved-state-cleanup/v1"}},
    {"command": "resource-clean", "phase": "PREPARED", "cleanup_plan": []},
    {"command": "resource-clean", "phase": "PREPARED", "cleanup_plan": {"contract": "unknown"}},
])
def test_rollout_unknown_transactions_do_not_request_preservation(capsys, journal):
    with GitRepository(linked=0) as repository:
        adopt_canonical(repository)
        store = MetadataStore(inventory(repository.repo).common_dir)
        transaction = str(uuid.uuid4())
        store.write_transaction(transaction, journal)
        before = metadata_bytes(store)
        diagnostic = rollout_error(repository, capsys)
        assert diagnostic["transaction_id"] == transaction
        assert diagnostic["command"] == journal.get("command")
        assert diagnostic["phase"] == journal.get("phase")
        assert diagnostic["error"]["code"] == "ROLLOUT_TRANSACTION_UNSUPPORTED"
        assert "preservation" not in diagnostic["error"]["message"]
        assert diagnostic["recovery_entry"] is None
        assert metadata_bytes(store) == before


def test_rollout_retains_unreadable_transaction_identity(capsys):
    with GitRepository(linked=0) as repository:
        adopt_canonical(repository)
        store = MetadataStore(inventory(repository.repo).common_dir)
        store.transactions_dir.mkdir(exist_ok=True)
        path = store.transactions_dir / "malformed.json"
        path.write_text("{invalid JSON")
        before = metadata_bytes(store)
        diagnostic = rollout_error(repository, capsys)
        assert diagnostic["transaction_id"] == "malformed"
        assert diagnostic["command"] is diagnostic["phase"] is None
        assert diagnostic["error"]["code"] == "ROLLOUT_TRANSACTION_UNSUPPORTED"
        assert "malformed" in diagnostic["error"]["message"]
        assert diagnostic["recovery_entry"] is None
        assert metadata_bytes(store) == before


@pytest.fixture
def interrupted(target, tmp_path, monkeypatch):
    repository, worktree, allocation = target
    state = worktree / ".venv/agent-workspace"
    state.mkdir(parents=True)
    (state / "opaque.sqlite").write_bytes(b"opaque test bytes\x00\xff")
    os.mkfifo(state / "pipe", 0o600)
    external = tmp_path / "sentinel"
    external.write_bytes(b"external intact")
    (state / "external").symlink_to(external)
    os.link(state / "opaque.sqlite", worktree / "backend/.venv/lib/cache-link")
    monkeypatch.setattr(p, "evidence_root", lambda: tmp_path / "preservations")
    planned = p.plan_preservation(repository.repo, worktree, allocation, "module-owner", 2)
    old = p.copy_preservation(planned)
    plan = lifecycle.cleanup_typed_disposable(
        repository.repo,
        target=worktree,
        owner="module-owner",
        expected_record_version=2,
        dry_run=True,
        preservation_receipt=Path(old["receipt"]),
        preservation_receipt_sha256=old["receipt_sha256"],
    )["decision"]["plan"]
    assert not plan["blockers"]
    # Synthetic 0.9.6 evidence deliberately contains no historical inode/member
    # graph. The fixture executes one FD-bound action, then retains the old shape.
    plan.pop("hardlink_topology")
    plan["plan_id"] = lifecycle._release_plan_id({k: v for k, v in plan.items() if k != "plan_id"})
    row = next(x for x in plan["resources"] if x["path"] == r.COMPLETED_RESOURCE)
    disposable.delete_typed_resource(worktree, r.COMPLETED_RESOURCE, row["evidence"])
    store = MetadataStore(inventory(repository.repo).common_dir)
    transaction = str(uuid.uuid4())
    actions = [{"path": r.COMPLETED_RESOURCE, "status": "COMPLETED"}] + [
        {"path": name, "status": "PENDING"} for name in r.REMAINING
    ]
    journal = lifecycle._transaction(
        command="resource-clean",
        transaction_id=transaction,
        repository_id=plan["repository_id"],
        target_path=worktree,
        allocation_id=allocation,
        branch="task-one",
        expected_head=plan["expected_head"],
        expected_record_version=2,
        phase="PREPARED",
        created_at=now_iso(),
    )
    journal.update(
        cleanup_plan=plan,
        actions=actions,
        failure={"code": "RESOURCE_CLEANUP_BLOCKED", "errno": None},
    )
    store.write_transaction(transaction, journal)
    store.write_completion_receipt(
        transaction,
        {
            "schema_version": 1,
            "receipt_id": transaction,
            "contract": "legacy-preserved-state-cleanup/v1",
            "result": "FAILED_OR_PARTIAL",
            "plan": plan,
            "actions": actions,
            "created_at": now_iso(),
        },
    )
    paths = [
        store.transactions_dir / f"{transaction}.json",
        store.completion_receipts_dir / f"{transaction}.json",
        Path(old["receipt"]),
        Path(old["receipt"]).parent / "manifest.json",
        Path(old["receipt"]).parent / "plan.json",
    ]
    old_bytes = {path: path.read_bytes() for path in paths}
    context = r.context(
        transaction,
        hashlib.sha256(old_bytes[paths[0]]).hexdigest(),
        hashlib.sha256(old_bytes[paths[1]]).hexdigest(),
    )
    return repository, worktree, allocation, store, context, old, old_bytes, external


def preserve(fixture):
    repository, worktree, allocation, _, context, _, _, _ = fixture
    plan = p.plan_preservation(
        repository.repo, worktree, allocation, "module-owner", 2, recovery=context
    )
    return p.copy_preservation(plan)


def prepare(fixture, new, *, execute=False):
    repository, worktree, _, _, context, _, _, _ = fixture
    options = dict(
        target=worktree,
        owner="module-owner",
        version=2,
        recovery=context,
        receipt_path=Path(new["receipt"]),
        receipt_sha=new["receipt_sha256"],
    )
    planned = r.prepare(repository.repo, **options, dry_run=True)
    if execute:
        return r.prepare(
            repository.repo,
            **options,
            dry_run=False,
            expected_plan_id=planned["decision"]["plan"]["plan_id"],
        )
    return planned


def test_rollout_requires_preservation_for_real_legacy_cleanup(interrupted, capsys):
    repository, worktree, _, store, context, _, old_bytes, external = interrupted
    before = metadata_bytes(store)
    diagnostic = rollout_error(repository, capsys)
    assert diagnostic["transaction_id"] == context["original_transaction_id"]
    assert diagnostic["command"] == "resource-clean" and diagnostic["phase"] == "PREPARED"
    assert diagnostic["error"] == {
        "code": "RE_PRESERVATION_RECOVERY_BLOCKED",
        "message": "unfinished cleanup requires an exact new preservation baseline",
    }
    assert diagnostic["recovery_entry"] == "resource-recover --stage prepare --dry-run"
    assert metadata_bytes(store) == before
    assert all(path.read_bytes() == raw for path, raw in old_bytes.items())
    assert not (worktree / r.COMPLETED_RESOURCE).exists()
    assert all((worktree / relative).exists() for relative in r.REMAINING)
    assert external.read_bytes() == b"external intact"


def test_rollout_retains_specific_cleanup_validation_error(interrupted, capsys):
    repository, _, _, store, _, _, old_bytes, _ = interrupted
    new = preserve(interrupted)
    before = metadata_bytes(store)
    diagnostic = rollout_error(repository, capsys, "--preservation-receipt", new["receipt"],
                               "--preservation-receipt-sha256", "0" * 64)
    assert diagnostic["command"] == "resource-clean"
    assert diagnostic["error"]["message"] == "preservation receipt bytes changed"
    assert diagnostic["recovery_entry"] == "resource-recover --stage prepare --dry-run"
    assert metadata_bytes(store) == before
    assert all(path.read_bytes() == raw for path, raw in old_bytes.items())


def test_represerve_unknown_history_resume_without_replay_and_release(interrupted, monkeypatch):
    repository, worktree, allocation, store, context, old, old_bytes, external = interrupted
    new = preserve(interrupted)
    assert new["generation"] != old["generation"]
    assert new["tree_sha256"] == old["tree_sha256"]
    compatible = r.rollout_check(
        repository.repo, receipt_path=Path(new["receipt"]), receipt_sha=new["receipt_sha256"]
    )
    assert compatible["decision"]["transactions"][0]["state"] == "RECOVERABLE_PARTIAL"
    assert not (store.root / "cleanup-recoveries").exists()
    prepared = prepare(interrupted, new, execute=True)["decision"]
    ref = prepared["recovery_receipt"]
    record, _ = e.read(Path(ref["path"]), ref["sha256"])
    assert record["historical_hardlink_cause"] == "NOT_PROVEN"
    assert record["safety_basis"] == "CURRENT_STATE_COMPLETELY_REPRESERVED"
    assert record["completed_action_will_replay"] is False
    with pytest.raises(ControllerError):
        prepare(interrupted, new, execute=True)
    with pytest.raises(FileExistsError):
        e.write(Path(ref["path"]).parent, "record.json", record)
    original_delete = lifecycle.delete_typed_resource
    deleted = []

    def guarded(root, relative, evidence):
        assert relative != r.COMPLETED_RESOURCE
        deleted.append(relative)
        return original_delete(root, relative, evidence)

    monkeypatch.setattr(lifecycle, "delete_typed_resource", guarded)
    result = r.resume(
        repository.repo, recovery_path=Path(ref["path"]), recovery_sha=ref["sha256"], dry_run=False
    )
    assert result["decision"]["code"] == "RECOVERY_CLEANED"
    assert deleted == r.REMAINING[:-1]
    assert r.completed(store, context["original_transaction_id"], verify_generations=True)
    terminal = r.rollout_check(repository.repo)
    assert terminal["decision"]["transactions"][0]["state"] == "RECOVERY_CLEANED"
    assert store.load().records[allocation].lifecycle == "HANDOFF_READY"
    lifecycle.release(
        repository.repo, target=worktree, expected_record_version=2, owner_release="module-owner"
    )
    assert store.load().records[allocation].lifecycle == "RELEASED"
    assert not worktree.exists()
    assert external.read_bytes() == b"external intact"
    assert all(path.read_bytes() == data for path, data in old_bytes.items())
    assert (Path(new["generation"]) / "receipt.json").is_file()
    assert not any(
        w["code"] == "INTERRUPTED_TRANSACTION" for w in doctor(repository.repo)["warnings"]
    )


@pytest.mark.parametrize("mutation", ["missing-baseline", "source-drift", "other-transaction"])
def test_rollout_still_blocks_unverified_transactions(interrupted, mutation):
    repository, worktree, _, store, _, _, old_bytes, _ = interrupted
    new = preserve(interrupted)
    if mutation == "source-drift":
        (worktree / ".venv/agent-workspace/opaque.sqlite").write_bytes(b"changed")
    elif mutation == "other-transaction":
        e.write(store.transactions_dir, str(uuid.uuid4()) + ".json", {})
    with pytest.raises(ControllerError):
        r.rollout_check(
            repository.repo,
            receipt_path=None if mutation == "missing-baseline" else Path(new["receipt"]),
            receipt_sha=new["receipt_sha256"],
        )
    assert all(path.read_bytes() == data for path, data in old_bytes.items())
    assert not (store.root / "cleanup-recoveries").exists()


@pytest.mark.parametrize(
    "mutation",
    [
        "no-new-generation",
        "content-before",
        "source-after",
        "old-receipt",
        "old-generation",
        "new-incomplete",
        "backend-reappeared",
        "frontend-changed",
        "old-journal",
    ],
)
def test_recovery_fail_closed(interrupted, mutation):
    _, worktree, _, _, _, old, old_bytes, _ = interrupted
    if mutation == "content-before":
        (worktree / ".venv/agent-workspace/opaque.sqlite").write_bytes(b"unreviewed content")
        with pytest.raises(ControllerError):
            preserve(interrupted)
        return
    new = old if mutation == "no-new-generation" else preserve(interrupted)
    if mutation == "source-after":
        (worktree / ".venv/agent-workspace/opaque.sqlite").write_bytes(b"new content")
    elif mutation in {"old-receipt", "old-journal"}:
        path = list(old_bytes)[1 if mutation == "old-receipt" else 0]
        path.chmod(0o600)
        path.write_bytes(old_bytes[path] + b" ")
    elif mutation == "old-generation":
        Path(old["generation"]).rename(Path(old["generation"]).with_name("saved-old"))
    elif mutation == "new-incomplete":
        (Path(new["generation"]) / "tree/agent-workspace/opaque.sqlite").unlink()
    elif mutation == "backend-reappeared":
        (worktree / r.COMPLETED_RESOURCE).mkdir()
    elif mutation == "frontend-changed":
        (worktree / "frontend/dist/assets/index.js").write_bytes(b"new build")
    with pytest.raises((ControllerError, OSError)):
        prepare(interrupted, new)
    assert worktree.exists()


def test_source_changes_between_represervation_plan_and_copy_block(interrupted):
    repository, worktree, allocation, _, context, _, _, _ = interrupted
    plan = p.plan_preservation(
        repository.repo, worktree, allocation, "module-owner", 2, recovery=context
    )
    (worktree / ".venv/agent-workspace/opaque.sqlite").write_bytes(
        b"changed during preservation window"
    )
    with pytest.raises(ControllerError):
        p.copy_preservation(plan)


def test_recovery_partial_failure_keeps_history_and_blocks_replay(interrupted, monkeypatch):
    repository, worktree, allocation, store, context, _, old_bytes, _ = interrupted
    new = preserve(interrupted)
    ref = prepare(interrupted, new, execute=True)["decision"]["recovery_receipt"]
    original_delete = lifecycle.delete_typed_resource

    def fail(root, relative, evidence):
        if relative == "frontend/node_modules":
            raise OSError(5, "fixture failure")
        return original_delete(root, relative, evidence)

    monkeypatch.setattr(lifecycle, "delete_typed_resource", fail)
    for _ in range(2):
        with pytest.raises(ControllerError):
            r.resume(
                repository.repo,
                recovery_path=Path(ref["path"]),
                recovery_sha=ref["sha256"],
                dry_run=False,
            )
    assert not r.completed(store, context["original_transaction_id"])
    assert store.load().records[allocation].lifecycle == "HANDOFF_READY"
    assert all(path.read_bytes() == data for path, data in old_bytes.items())
    assert not (worktree / "frontend/dist").exists()
    assert (worktree / ".venv").exists()


@pytest.mark.parametrize("pair", ["typed-preserved", "typed-typed", "same-resource"])
def test_cross_resource_graph_and_exact_link_effects(preserved, pair):
    repository, worktree, allocation, copied, _ = preserved
    source = worktree / "frontend/dist/assets/index.js"
    destination = (
        worktree
        / (
            {
                "typed-preserved": ".venv/agent-workspace/build-link",
                "typed-typed": "frontend/node_modules/vite/build-link",
                "same-resource": "frontend/dist/assets/second.js",
            }[pair]
        )
    )
    os.link(source, destination)
    # The state changed intentionally before the new fixture receipt was created.
    if pair == "typed-preserved":
        plan = p.plan_preservation(repository.repo, worktree, allocation, "module-owner", 2)
        new = p.copy_preservation(plan)
        copied.update(new)
    planned = dry_run(preserved)["plan"]
    graph = planned["hardlink_topology"]
    key = f"{source.stat().st_dev}:{source.stat().st_ino}"
    assert len(graph["groups"][key]["members"]) == 2
    result = clean(preserved, planned)
    assert result["decision"]["code"] == "PRESERVED_STATE_CLEANED"
    store = MetadataStore(inventory(repository.repo).common_dir)
    receipt, _ = e.read(
        store.completion_receipts_dir / (result["decision"]["receipt_id"] + ".json")
    )
    dist = next(a for a in receipt["action_evidence"] if a["resource"] == "frontend/dist")
    after, _ = e.read(Path(dist["after"]["path"]), dist["after"]["sha256"])
    if pair != "same-resource":
        assert after["mutation"]["mutations"][key]["removed_links"] == 1
        assert after["mutation"]["mutations"][key]["after"][8] == 1


def test_out_of_plan_link_requires_pinned_survivor_evidence(preserved, tmp_path):
    _, worktree, _, _, _ = preserved
    os.link(worktree / "frontend/dist/assets/index.js", tmp_path / "unowned-link")
    plan = dry_run(preserved)["plan"]
    assert not plan["blockers"]
    graph = plan["hardlink_topology"]
    assert graph["contract"] == hardlinks.EXTERNAL_CONTRACT
    source = worktree / "frontend/dist/assets/index.js"
    group = graph["groups"][f"{source.stat().st_dev}:{source.stat().st_ino}"]
    assert group["external_links"] == 1


@pytest.mark.parametrize(
    "mutation", ["ctime-only", "extra-unlink", "content", "size", "inode-replacement"]
)
def test_unrecorded_topology_mutations_block(preserved, mutation):
    _, worktree, _, _, _ = preserved
    source = worktree / "frontend/dist/assets/index.js"
    link = worktree / "frontend/dist/assets/second.js"
    os.link(source, link)
    graph = hardlinks.inventory(worktree, ["frontend/dist"])
    if mutation == "ctime-only":
        source.chmod(source.stat().st_mode)
    elif mutation == "extra-unlink":
        link.unlink()
    elif mutation in {"content", "size"}:
        source.write_bytes(b"X" * (source.stat().st_size if mutation == "content" else 1))
    else:
        value = source.read_bytes()
        source.unlink()
        source.write_bytes(value)
    with pytest.raises(ControllerError):
        hardlinks.verify(worktree, graph)


def test_topology_race_after_last_plan_check_blocks_before_unlink(preserved, monkeypatch, tmp_path):
    _, worktree, _, _, _ = preserved
    plan = dry_run(preserved)["plan"]
    original = lifecycle.delete_typed_resource
    outside = tmp_path / "late-link"

    def swapped(root, relative, evidence):
        source = root / "backend/.venv/bin/python"
        os.link(source, outside)
        return original(root, relative, evidence)

    monkeypatch.setattr(lifecycle, "delete_typed_resource", swapped)
    with pytest.raises(ControllerError):
        clean(preserved, plan)
    assert (worktree / "backend/.venv/bin/python").is_file()
    assert outside.read_bytes() == (worktree / "backend/.venv/bin/python").read_bytes()


def test_transition_rejects_unrelated_ctime_change(preserved):
    _, worktree, _, _, _ = preserved
    graph = hardlinks.inventory(worktree, ["frontend/dist", "frontend/node_modules"])
    after = copy.deepcopy(graph)
    after["resources"].remove("frontend/dist")
    del after["anchors"]["frontend/dist"]
    after["groups"] = {
        k: g for k, g in after["groups"].items() if g["members"][0][0] != "frontend/dist"
    }
    next(iter(after["groups"].values()))["identity"][5] += 1
    after["sha256"] = p._sha({k: v for k, v in after.items() if k != "sha256"})
    with pytest.raises(ControllerError):
        hardlinks.transition(graph, after, "frontend/dist")
