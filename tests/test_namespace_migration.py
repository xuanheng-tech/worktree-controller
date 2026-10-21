"""The legacy namespace migration entry point: ``codex-worktree/v1`` becomes ``worktree-controller/v1``.

A migration is only worth trusting if the five shapes it can meet are each pinned by a
test, if an interrupt leaves a replayable rather than a half-moved repository, and if no
path can leave two namespaces both claiming authority.  Everything here is driven through
the published functions and CLI, over trees the Controller itself wrote - never over
hand-copied metadata.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

from tests.test_auto_release import POLICY_MANUAL
from tests.test_branch_retirement import _released_source
from tests.test_controlled import _configure_remote, adopt_canonical
from tests.test_shadow import GitRepository, git
from worktree_controller import cleanup_evidence as evidence
from worktree_controller import metadata_relocation as relocation
from worktree_controller import namespace_migration as migration
from worktree_controller.activation_layout import lease_bytes
from worktree_controller.branch_retirement import plan_branch_retirement
from worktree_controller.git_backend import inventory
from worktree_controller.lifecycle import review
from worktree_controller.models import ControllerError
from worktree_controller.store import ROOT_NAME, VERSION_NAME, MetadataStore
from worktree_controller.writer_lease import acquire_writer_lease

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "src"


def _common(repository: GitRepository) -> Path:
    return inventory(repository.repo).common_dir


def _namespace(repository: GitRepository, name: str) -> Path:
    return _common(repository) / name / VERSION_NAME


def _leave_behind(repository: GitRepository, *, lease: bool = False, policy: bool = False) -> Path:
    """Put a repository's real state where a pre-migration host left it.

    The documents are the ones the Controller wrote; only the namespace name, the writer
    lease schema and the policy path are historical.
    """

    if lease:
        acquired = acquire_writer_lease(
            repository.repo,
            target=repository.linked[0],
            client_id="codex",
            holder_pid=os.getpid(),
        )
        assert acquired["decision"]["code"] == "WRITER_LEASE_ACQUIRED", acquired["decision"]
    legacy = _namespace(repository, migration.LEGACY_ROOT_NAME)
    legacy.parent.mkdir(exist_ok=True)
    os.rename(_namespace(repository, ROOT_NAME), legacy)
    for path in sorted(legacy.glob("writer-leases/*.json")):
        path.write_bytes(lease_bytes(path.read_bytes(), reverse=True))
    if policy:
        historic = repository.repo / ".codex" / "worktree-policy.toml"
        historic.parent.mkdir(exist_ok=True)
        os.rename(repository.repo / ".agents" / "worktree-policy.toml", historic)
    return legacy


def _copy_into_current(repository: GitRepository, legacy: Path) -> Path:
    current = _namespace(repository, ROOT_NAME)
    current.mkdir(parents=True)
    for path in sorted(legacy.rglob("*")):
        relative = path.relative_to(legacy)
        if path.is_dir():
            (current / relative).mkdir(exist_ok=True)
            continue
        (current / relative).write_bytes(path.read_bytes())
    return current


def _merged(root: Path) -> dict[str, bytes]:
    """The destination namespace without the migration's own bookkeeping."""

    return {
        relative: raw
        for relative, raw in _contents(root).items()
        if migration.RECEIPTS_DIRECTORY not in relative
        and relative not in {relocation.RECORD, "repo.lock"}
    }


def _authoritative(relative: str, raw: bytes) -> bytes:
    """What the runtime must read at the destination: a legacy lease in its current schema."""

    document = json.loads(raw)
    if relative.startswith("writer-leases/") and document.get("schema_version") == 1:
        return lease_bytes(raw)
    return raw


def _contents(root: Path) -> dict[str, bytes]:
    if not root.exists():
        return {}
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _code(action) -> str:
    with pytest.raises(ControllerError) as captured:
        action()
    return captured.value.code


def _crash_migration(
    repository: GitRepository, plan_id: str, fault: str, *, policy_from: Path | None = None
) -> None:
    from tests.test_controlled import _cli

    arguments = [
        "namespace-migrate",
        "--repo",
        str(repository.repo),
        "--actor",
        "operator",
        "--expected-plan-id",
        plan_id,
        "--json",
    ]
    if policy_from is not None:
        arguments += ["--policy-from", str(policy_from)]
    result = _cli(arguments, crash_at=fault)
    assert result.returncode == 97, result.stdout + result.stderr


def _released(repository: GitRepository) -> tuple[str, str]:
    _configure_remote(repository)
    adopt_canonical(repository)
    return _released_source(repository, "namespace")


@pytest.fixture
def legacy_repository() -> GitRepository:
    with GitRepository(linked=1) as repository:
        repository.adopt_all()
        _leave_behind(repository, lease=True)
        yield repository


# --- the five shapes ------------------------------------------------------------------


def test_old_only_namespace_migrates_and_the_runtime_reads_it(legacy_repository) -> None:
    repository = legacy_repository
    namespace = _namespace(repository, migration.LEGACY_ROOT_NAME)
    before = {key: value for key, value in _contents(namespace).items() if key != "repo.lock"}
    plan = migration.plan_namespace_migration(repository.repo)["decision"]
    assert plan["code"] == migration.REQUIRED
    assert sorted(plan["plan"]["moves"]) == sorted(before)

    result = migration.migrate_namespace(
        repository.repo, actor="operator", expected_plan_id=plan["plan"]["plan_id"]
    )["decision"]
    assert result["code"] == migration.MIGRATED
    assert result["changed"] is True
    assert set(result["receipt"]["moved"]) | {migration.AUTHORITY_FILE} == set(before)

    current = _namespace(repository, ROOT_NAME)
    expected = {relative: _authoritative(relative, raw) for relative, raw in before.items()}
    assert _merged(current) == expected
    assert not namespace.exists()
    assert MetadataStore(_common(repository)).load().records


def test_registration_prune_receipt_participates_in_migration_digest(legacy_repository) -> None:
    from worktree_controller.controller import doctor, status

    repository = legacy_repository
    legacy = _namespace(repository, migration.LEGACY_ROOT_NAME)
    receipt = legacy / "registration-prune-receipts" / "old-audit.json"
    receipt.parent.mkdir()
    original = b'{"contract":"registration-prune/v1","receipt_id":"old-audit"}\n'
    receipt.write_bytes(original)

    reviewed = migration.plan_namespace_migration(repository.repo)["decision"]["plan"]
    relative = "registration-prune-receipts/old-audit.json"
    assert relative in reviewed["moves"]
    receipt.write_bytes(original.replace(b"old-audit", b"changed-audit"))
    changed = migration.plan_namespace_migration(repository.repo)["decision"]["plan"]
    assert changed["merged_digest"] != reviewed["merged_digest"]
    assert (
        _code(
            lambda: migration.migrate_namespace(
                repository.repo, actor="operator", expected_plan_id=reviewed["plan_id"]
            )
        )
        == "NAMESPACE_PLAN_DRIFT"
    )

    receipt.write_bytes(original)
    result = migration.migrate_namespace(
        repository.repo, actor="operator", expected_plan_id=reviewed["plan_id"]
    )["decision"]
    assert result["code"] == migration.MIGRATED
    assert relative in result["receipt"]["moved"]
    assert (_namespace(repository, ROOT_NAME) / relative).read_bytes() == original
    assert migration.verify_namespace(repository.repo)["decision"]["code"] == "NAMESPACE_VERIFIED"
    assert status(repository.repo)["errors"] == []
    assert doctor(repository.repo)["decision"]["code"] == "PASS"


def test_new_only_namespace_is_an_idempotent_no_op() -> None:
    with GitRepository(linked=0, policy_text=POLICY_MANUAL) as repository:
        adopt_canonical(repository)
        current = _namespace(repository, ROOT_NAME)
        before = _contents(current)

        plan = migration.plan_namespace_migration(repository.repo)["decision"]
        assert plan["code"] == migration.CURRENT
        assert plan["plan"]["moves"] == [] and plan["plan"]["redundant"] == []
        result = migration.migrate_namespace(
            repository.repo, actor="operator", expected_plan_id=plan["plan"]["plan_id"]
        )["decision"]
        assert result["code"] == migration.CURRENT and result["changed"] is False
        assert _contents(current) == before
        assert not (current / migration.RECEIPTS_DIRECTORY).exists()


def test_equivalent_namespaces_converge_and_clear_the_legacy_copies(legacy_repository) -> None:
    repository = legacy_repository
    legacy = _namespace(repository, migration.LEGACY_ROOT_NAME)
    before = {key: value for key, value in _contents(legacy).items() if key != "repo.lock"}
    _copy_into_current(repository, legacy)

    plan = migration.plan_namespace_migration(repository.repo)["decision"]
    assert plan["code"] == migration.CONVERGENCE
    assert plan["plan"]["moves"] == []
    result = migration.migrate_namespace(
        repository.repo, actor="operator", expected_plan_id=plan["plan"]["plan_id"]
    )["decision"]
    assert result["code"] == migration.CONVERGED
    assert result["receipt"]["redundant_cleared"]
    assert not legacy.exists()
    assert _merged(_namespace(repository, ROOT_NAME)) == {
        relative: _authoritative(relative, raw) for relative, raw in before.items()
    }


def test_a_destination_lease_nobody_can_parse_is_rewritten_not_kept(
    legacy_repository,
) -> None:
    """Convergence must produce a readable namespace, not merely an empty legacy one.

    A legacy copy that was once duplicated by hand leaves the destination holding the
    historical schema.  Reporting that as "already current" would strand the repository
    behind a parser that refuses it, so the authoritative form is rewritten in place.
    """

    repository = legacy_repository
    legacy = _namespace(repository, migration.LEGACY_ROOT_NAME)
    name = sorted(path.name for path in (legacy / "writer-leases").glob("*.json"))[0]
    historical = (legacy / "writer-leases" / name).read_bytes()
    _copy_into_current(repository, legacy)
    (legacy / "writer-leases" / name).unlink()
    (legacy / "writer-leases").rmdir()
    assert json.loads(
        (_namespace(repository, ROOT_NAME) / "writer-leases" / name).read_bytes()
    ) == json.loads(historical)
    plan = migration.plan_namespace_migration(repository.repo)["decision"]
    assert plan["code"] == migration.CONVERGENCE
    result = migration.migrate_namespace(
        repository.repo, actor="operator", expected_plan_id=plan["plan"]["plan_id"]
    )["decision"]
    assert result["receipt"]["destination_rewrites"] == [f"writer-leases/{name}"]
    value = json.loads((_namespace(repository, ROOT_NAME) / "writer-leases" / name).read_bytes())
    assert value["schema_version"] == 2 and value["holder"]["client_id"] == "codex"
    assert MetadataStore(_common(repository)).load().writer_leases


def test_conflicting_namespaces_fail_closed_without_writing(legacy_repository) -> None:
    repository = legacy_repository
    current = _copy_into_current(repository, _namespace(repository, migration.LEGACY_ROOT_NAME))
    victim = sorted((current / "records").glob("*.json"))[0]
    moved = json.loads(victim.read_bytes())
    moved["owner"] = "a-different-owner"
    victim.write_bytes((json.dumps(moved, indent=2, sort_keys=True) + "\n").encode())
    legacy_contents = _contents(_namespace(repository, migration.LEGACY_ROOT_NAME))
    current_contents = _contents(current)

    code = _code(lambda: migration.plan_namespace_migration(repository.repo))
    assert code == "NAMESPACE_CONFLICT"
    with pytest.raises(ControllerError) as captured:
        migration.migrate_namespace(
            repository.repo,
            actor="operator",
            expected_plan_id=migration._digest(legacy_contents),
        )
    assert captured.value.code == "NAMESPACE_CONFLICT"
    assert _contents(_namespace(repository, migration.LEGACY_ROOT_NAME)) == legacy_contents
    assert _contents(current) == current_contents
    assert not (current / migration.RECEIPTS_DIRECTORY).exists()


def test_a_bootstrapped_destination_identity_is_a_conflict(legacy_repository) -> None:
    """The hazard the destination fence exists for: a second identity beside the old one.

    ``adopt --allow-create`` mints a fresh ``repo.json`` when the namespace is absent, so
    a repository whose legacy tree was never migrated can gain a live, empty current
    namespace at any moment.  Two authorities is never converged into one.
    """

    repository = legacy_repository
    from worktree_controller.controller import adopt

    adopt(
        repository.repo,
        target=repository.linked[0],
        task_key="bootstrapped",
        authority_key="bootstrapped",
        module="late",
        role="feature",
        owner="someone-else",
        lifecycle="ACTIVE",
    )
    assert (
        _code(lambda: migration.plan_namespace_migration(repository.repo)) == "NAMESPACE_CONFLICT"
    )


# --- evidence and reference integrity --------------------------------------------------


def test_migration_publishes_relocation_evidence_for_immutable_references(
    legacy_repository,
) -> None:
    repository = legacy_repository
    legacy = _namespace(repository, migration.LEGACY_ROOT_NAME)
    transaction = str(uuid.uuid4())
    stored = evidence.write(
        legacy / "completion-receipts", f"{transaction}.json", {"phase": "DONE"}
    )
    tree = legacy / "source-cleanup" / transaction
    tree.mkdir(parents=True)
    before = evidence.write(tree, "0000-before.json", {"path": "src/plain.py"})

    migration.migrate_namespace(
        repository.repo,
        actor="operator",
        expected_plan_id=migration.plan_namespace_migration(repository.repo)["decision"]["plan"][
            "plan_id"
        ],
    )
    current = _namespace(repository, ROOT_NAME)
    record = json.loads((current / relocation.RECORD).read_bytes())
    assert record["previous_root"] == str(legacy) and not legacy.exists()
    assert record["files"] == relocation.inventory(current)
    assert record["files"][f"completion-receipts/{transaction}.json"] == stored["sha256"]
    resolved = relocation.relocated(
        legacy / "source-cleanup" / transaction / "0000-before.json", before["sha256"]
    )
    assert resolved == current / "source-cleanup" / transaction / "0000-before.json"
    assert (
        relocation.relocated(
            legacy / "completion-receipts" / f"{transaction}.json", stored["sha256"]
        )
        == current / "completion-receipts" / f"{transaction}.json"
    )
    assert relocation._record(current)["contract"] == relocation.CONTRACT


def test_reference_resolution_refuses_a_digest_that_is_not_the_recorded_one(
    legacy_repository,
) -> None:
    repository = legacy_repository
    legacy = _namespace(repository, migration.LEGACY_ROOT_NAME)
    transaction = str(uuid.uuid4())
    evidence.write(legacy / "completion-receipts", f"{transaction}.json", {"phase": "DONE"})
    plan = migration.plan_namespace_migration(repository.repo)["decision"]["plan"]
    migration.migrate_namespace(repository.repo, actor="operator", expected_plan_id=plan["plan_id"])

    code = _code(
        lambda: relocation.relocated(
            legacy / "completion-receipts" / f"{transaction}.json", "0" * 64
        )
    )
    assert code == "METADATA_RELOCATION_BLOCKED"


def test_retirement_plans_and_receipts_keep_resolving_after_the_move() -> None:
    """Migration must not change retirement execution semantics.

    The plan identity is a digest over authority content, not over namespace paths, so a
    released branch's plan has to survive the move byte for byte and stay revalidatable.
    """

    with GitRepository(linked=0, policy_text=POLICY_MANUAL) as repository:
        allocation, head = _released(repository)
        planned = plan_branch_retirement(
            repository.repo, allocation_id=allocation, remote="origin", integrated_into="main"
        )["decision"]
        assert planned["code"] == "BRANCH_RETIREMENT_READY", planned
        plan_id = planned["plan"]["plan_id"]
        before = _contents(_namespace(repository, ROOT_NAME))
        _leave_behind(repository)

        result = migration.migrate_namespace(
            repository.repo,
            actor="operator",
            expected_plan_id=migration.plan_namespace_migration(repository.repo)["decision"][
                "plan"
            ]["plan_id"],
        )["decision"]
        assert result["code"] == migration.MIGRATED
        documented = json.loads(
            (
                _namespace(repository, ROOT_NAME) / "branch-retirement-plans" / f"{plan_id}.json"
            ).read_bytes()
        )
        assert documented["plan_id"] == plan_id and documented["expected_oid"] == head
        current = {
            relative: raw
            for relative, raw in _contents(_namespace(repository, ROOT_NAME)).items()
            if migration.RECEIPTS_DIRECTORY not in relative and relative != relocation.RECORD
        }
        assert current == before


def test_schema_one_writer_leases_become_parseable_after_migration(legacy_repository) -> None:
    repository = legacy_repository
    legacy = _namespace(repository, migration.LEGACY_ROOT_NAME)
    lease = sorted((legacy / "writer-leases").glob("*.json"))[0]
    assert json.loads(lease.read_bytes())["schema_version"] == 1
    assert "provider" in json.loads(lease.read_bytes())["holder"]

    result = migration.migrate_namespace(
        repository.repo,
        actor="operator",
        expected_plan_id=migration.plan_namespace_migration(repository.repo)["decision"]["plan"][
            "plan_id"
        ],
    )["decision"]
    assert result["receipt"]["lease_conversions"] == [f"writer-leases/{lease.name}"]
    moved = _namespace(repository, ROOT_NAME) / "writer-leases" / lease.name
    value = json.loads(moved.read_bytes())
    assert value["schema_version"] == 2 and value["holder"]["client_id"] == "codex"
    assert MetadataStore(_common(repository)).load().writer_leases


def test_legacy_policy_authority_moves_with_the_namespace() -> None:
    with GitRepository(linked=0, policy_text=POLICY_MANUAL) as repository:
        adopt_canonical(repository)
        historic = repository.repo / ".codex" / "worktree-policy.toml"
        _leave_behind(repository, policy=True)
        assert historic.is_file()
        digest = historic.read_bytes()

        plan = migration.plan_namespace_migration(repository.repo)["decision"]["plan"]
        assert plan["policy"]["move"] == str(historic)
        migration.migrate_namespace(
            repository.repo, actor="operator", expected_plan_id=plan["plan_id"]
        )
        assert not historic.exists()
        assert (repository.repo / ".agents" / "worktree-policy.toml").read_bytes() == digest
        assert git(repository.repo, "status", "--porcelain") == ""


# --- failure modes ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("disturb", "code"),
    [
        ("non-json", "NAMESPACE_EVIDENCE_INVALID"),
        ("symlink", "NAMESPACE_EVIDENCE_INVALID"),
        ("corrupt", "NAMESPACE_EVIDENCE_INVALID"),
        ("no-authority", "NAMESPACE_IDENTITY_MISSING"),
        ("no-lock", "NAMESPACE_LOCK_MISSING"),
        ("relocation", "NAMESPACE_RELOCATION_PRESENT"),
        ("unmanaged", migration.UNMANAGED),
    ],
)
def test_legacy_state_the_controller_cannot_trust_fails_closed(
    legacy_repository, disturb: str, code: str
) -> None:
    repository = legacy_repository
    legacy = _namespace(repository, migration.LEGACY_ROOT_NAME)
    if disturb == "non-json":
        (legacy / "notes.txt").write_bytes(b"what is this")
    elif disturb == "symlink":
        (legacy / "records" / "escape.json").symlink_to(legacy / "repo.json")
    elif disturb == "corrupt":
        sorted((legacy / "records").glob("*.json"))[0].write_bytes(b'{"lifecycle": ')
    elif disturb == "no-authority":
        (legacy / "repo.json").unlink()
    elif disturb == "no-lock":
        (legacy / "repo.lock").unlink()
    elif disturb == "relocation":
        (legacy / relocation.RECORD).write_bytes(b"{}")
    elif disturb == "unmanaged":
        import shutil

        shutil.rmtree(_common(repository) / migration.LEGACY_ROOT_NAME)
    before = _contents(legacy)
    assert _code(lambda: migration.plan_namespace_migration(repository.repo)) == code
    if disturb != "unmanaged":
        assert _contents(legacy) == before


def test_an_unfinished_legacy_transaction_blocks_the_transition_in_the_fence(
    legacy_repository,
) -> None:
    """A journal nobody finished is evidence of live work, so the move refuses it.

    The advisory plan cannot answer that question without waiting behind whatever writer
    holds the namespace lock, so it defers it; the fenced re-derivation answers it before
    anything destructive happens.
    """

    repository = legacy_repository
    legacy = _namespace(repository, migration.LEGACY_ROOT_NAME)
    MetadataStore(_common(repository), _migration_root=legacy)._atomic_json(
        legacy / "transactions" / f"{uuid.uuid4()}.json",
        {
            "transaction_id": "not-a-uuid",
            "repository_id": "someone-else",
            "allocation_id": "x",
            "target_path": str(repository.root),
            "command": "acquire",
            "schema_version": 4,
            "phase": "NO_SUCH_PHASE",
        },
    )
    plan = migration.plan_namespace_migration(repository.repo)["decision"]
    assert plan["code"] == migration.REQUIRED
    assert plan["plan"]["preflight"] == "under_fence"
    assert (
        _code(
            lambda: migration.migrate_namespace(
                repository.repo, actor="operator", expected_plan_id=plan["plan"]["plan_id"]
            )
        )
        == "NAMESPACE_UNSAFE"
    )
    assert legacy.is_dir()


def test_a_held_namespace_lock_never_makes_a_plan_wait(legacy_repository, tmp_path) -> None:
    """The read-only verbs stay readable while a writer owns the section."""

    repository = legacy_repository
    release = tmp_path / "plan-holder"
    holder = _holder(repository, release, migration.LEGACY_ROOT_NAME)
    try:
        _wait_for(release.with_name("plan-holder.ready"), holder)
        started = time.monotonic()
        plan = migration.plan_namespace_migration(repository.repo)["decision"]
        assert plan["code"] == migration.REQUIRED
        assert time.monotonic() - started < 5, "a read-only plan queued behind a writer"
        plan_id = plan["plan"]["plan_id"]
        assert (
            _code(
                lambda: migration.migrate_namespace(
                    repository.repo, actor="operator", expected_plan_id=plan_id
                )
            )
            == "NAMESPACE_BUSY"
        )
        assert _namespace(repository, migration.LEGACY_ROOT_NAME).is_dir()
    finally:
        release.touch()
        holder.wait(timeout=60)
        holder.stdout.close()


def test_an_absent_legacy_namespace_and_no_current_one_reports_unmanaged() -> None:
    with GitRepository(linked=0, policy_text=POLICY_MANUAL) as repository:
        assert (
            _code(lambda: migration.plan_namespace_migration(repository.repo))
            == migration.UNMANAGED
        )


def test_only_the_exact_plan_may_be_applied(legacy_repository) -> None:
    repository = legacy_repository
    plan = migration.plan_namespace_migration(repository.repo)["decision"]["plan"]
    assert (
        _code(
            lambda: migration.migrate_namespace(
                repository.repo, actor="operator", expected_plan_id="0" * 64
            )
        )
        == "NAMESPACE_PLAN_DRIFT"
    )
    assert (
        _code(
            lambda: migration.migrate_namespace(repository.repo, expected_plan_id=plan["plan_id"])
        )
        == "INVALID_REQUEST"
    )
    legacy = _namespace(repository, migration.LEGACY_ROOT_NAME)
    victim = sorted((legacy / "records").glob("*.json"))[0]
    payload = json.loads(victim.read_bytes())
    payload["owner"] = "changed-after-the-plan"
    victim.write_bytes((json.dumps(payload, indent=2, sort_keys=True) + "\n").encode())
    assert (
        _code(
            lambda: migration.migrate_namespace(
                repository.repo, actor="operator", expected_plan_id=plan["plan_id"]
            )
        )
        == "NAMESPACE_PLAN_DRIFT"
    )
    assert legacy.is_dir()


def test_an_unsafe_namespace_name_is_refused(legacy_repository) -> None:
    repository = legacy_repository
    assert (
        _code(
            lambda: migration.plan_namespace_migration(repository.repo, legacy_namespace="../other")
        )
        == "NAMESPACE_UNSUPPORTED"
    )
    assert (
        _code(
            lambda: migration.plan_namespace_migration(repository.repo, legacy_namespace=ROOT_NAME)
        )
        == "INVALID_REQUEST"
    )


# --- replay, interruption and concurrency ---------------------------------------------


def test_repeated_execution_reports_the_original_receipt(legacy_repository) -> None:
    repository = legacy_repository
    plan_id = migration.plan_namespace_migration(repository.repo)["decision"]["plan"]["plan_id"]
    first = migration.migrate_namespace(
        repository.repo, actor="operator", expected_plan_id=plan_id
    )["decision"]
    directory = _namespace(repository, ROOT_NAME) / migration.RECEIPTS_DIRECTORY
    settled = directory / f"{plan_id}.json"
    raw = settled.read_bytes()
    digest = first["receipt"]["merged_digest"]

    again = migration.migrate_namespace(
        repository.repo, actor="operator", expected_plan_id=plan_id
    )["decision"]
    assert again["idempotent"] is True and again["changed"] is False
    assert again["code"] == migration.MIGRATED
    assert again["receipt"]["merged_digest"] == digest
    assert settled.read_bytes() == raw
    assert sorted(path.name for path in directory.glob("*.json")) == [
        f"{plan_id}.json",
        f"{plan_id}.prepared.json",
    ]


def test_migration_receipts_are_immutable(legacy_repository) -> None:
    repository = legacy_repository
    plan_id = migration.plan_namespace_migration(repository.repo)["decision"]["plan"]["plan_id"]
    migration.migrate_namespace(repository.repo, actor="operator", expected_plan_id=plan_id)
    settled = _namespace(repository, ROOT_NAME) / migration.RECEIPTS_DIRECTORY / f"{plan_id}.json"
    assert oct(settled.stat().st_mode & 0o777) == "0o400"
    with pytest.raises(OSError):
        settled.write_bytes(b'{"forged": true}')
    listed = migration.namespace_migration_receipts(repository.repo)["decision"]
    assert {row["phase"] for row in listed["receipts"]} == {"PREPARED", "SETTLED"}
    assert {row["plan_id"] for row in listed["receipts"]} == {plan_id}


@pytest.mark.parametrize(
    "fault",
    (
        "namespace_receipt_prepared",
        "namespace_conversion_temp_partial",
        "namespace_converted_lease_published",
        "namespace_entries_moved",
        "namespace_switched",
        "namespace_record_written",
        "namespace_receipt_settled",
    ),
)
def test_an_interrupted_migration_replays_to_the_finished_state(
    legacy_repository, fault: str
) -> None:
    repository = legacy_repository
    legacy = _namespace(repository, migration.LEGACY_ROOT_NAME)
    plan_id = migration.plan_namespace_migration(repository.repo)["decision"]["plan"]["plan_id"]
    arguments = [
        "namespace-migrate",
        "--repo",
        str(repository.repo),
        "--actor",
        "operator",
        "--expected-plan-id",
        plan_id,
        "--json",
    ]
    from tests.test_controlled import _cli

    crashed = _cli(arguments, crash_at=fault)
    assert crashed.returncode == 97, crashed.stderr

    finished = _cli(arguments)
    assert finished.returncode == 0, finished.stdout + finished.stderr
    result = json.loads(finished.stdout)["decision"]
    assert result["code"] == migration.MIGRATED
    assert result["changed"] is (fault != "namespace_receipt_settled")
    assert not legacy.exists()
    verdict = migration.verify_namespace(repository.repo)["decision"]
    assert verdict["code"] == "NAMESPACE_VERIFIED", verdict
    assert MetadataStore(_common(repository)).load().records


def test_partial_conversion_scratch_cannot_change_the_replay_plan(legacy_repository) -> None:
    repository = legacy_repository
    legacy = _namespace(repository, migration.LEGACY_ROOT_NAME)
    current = _namespace(repository, ROOT_NAME)
    plan_id = migration.plan_namespace_migration(repository.repo)["decision"]["plan"]["plan_id"]
    _crash_migration(repository, plan_id, "namespace_conversion_temp_partial")
    lease = next((legacy / "writer-leases").glob("*.json"))
    assert not (current / "writer-leases" / lease.name).exists()
    assert list((current / "writer-leases").glob(".*.tmp"))
    advisory = migration.plan_namespace_migration(repository.repo)["decision"]
    assert advisory["recovery_required"] is True
    assert advisory["plan"]["plan_id"] == plan_id
    assert (
        migration.migrate_namespace(repository.repo, actor="operator", expected_plan_id=plan_id)[
            "decision"
        ]["code"]
        == migration.MIGRATED
    )
    assert not list((current / "writer-leases").glob(".*.tmp"))


@pytest.mark.parametrize(
    "fault", ["namespace_rewrite_temp_fsynced", "namespace_destination_rewritten"]
)
def test_a_converged_lease_rewrite_replays_with_ignored_scratch(
    legacy_repository, fault: str
) -> None:
    repository = legacy_repository
    legacy = _namespace(repository, migration.LEGACY_ROOT_NAME)
    current = _copy_into_current(repository, legacy)
    plan = migration.plan_namespace_migration(repository.repo)["decision"]
    assert plan["code"] == migration.CONVERGENCE
    plan_id = plan["plan"]["plan_id"]
    _crash_migration(repository, plan_id, fault)
    if fault == "namespace_rewrite_temp_fsynced":
        assert list((current / "writer-leases").glob(".*.tmp"))
    assert (
        migration.plan_namespace_migration(repository.repo)["decision"]["plan"]["plan_id"]
        == plan_id
    )
    result = migration.migrate_namespace(
        repository.repo, actor="operator", expected_plan_id=plan_id
    )["decision"]
    assert result["code"] == migration.CONVERGED
    assert not legacy.exists()
    assert not list((current / "writer-leases").glob(".*.tmp"))
    assert migration.verify_namespace(repository.repo)["decision"]["code"] == "NAMESPACE_VERIFIED"


@pytest.mark.parametrize("fault", ["namespace_entries_moved", "namespace_policy_moved"])
def test_custom_policy_source_replays_without_repeating_the_argument(
    legacy_repository, fault: str
) -> None:
    repository = legacy_repository
    source = repository.repo / "custom-policy" / "worktree-policy.toml"
    source.parent.mkdir()
    os.rename(repository.repo / ".agents" / "worktree-policy.toml", source)
    original = source.read_bytes()
    plan_id = migration.plan_namespace_migration(repository.repo, policy_from=source)["decision"][
        "plan"
    ]["plan_id"]
    _crash_migration(repository, plan_id, fault, policy_from=source)
    advisory = migration.plan_namespace_migration(repository.repo)["decision"]
    assert advisory["recovery_required"] is True
    assert advisory["plan"]["plan_id"] == plan_id
    result = migration.migrate_namespace(
        repository.repo, actor="operator", expected_plan_id=plan_id
    )["decision"]
    assert result["code"] == migration.MIGRATED
    assert not source.exists()
    assert (repository.repo / ".agents" / "worktree-policy.toml").read_bytes() == original


def test_a_post_switch_controller_review_does_not_orphan_prepared_evidence(
    legacy_repository,
) -> None:
    repository = legacy_repository
    plan_id = migration.plan_namespace_migration(repository.repo)["decision"]["plan"]["plan_id"]
    _crash_migration(repository, plan_id, "namespace_switched")
    current = _namespace(repository, ROOT_NAME)
    snapshot = MetadataStore(_common(repository)).load()
    item = next(
        item for item in inventory(repository.repo).worktrees if item.path == repository.linked[0]
    )
    record = snapshot.records[snapshot.bindings[item.key]]
    updated = review(
        repository.repo,
        target=item.path,
        expected_record_version=record.record_version,
        owner=record.owner,
    )["decision"]
    assert updated["code"] == "REVIEWED"
    advisory = migration.plan_namespace_migration(repository.repo)["decision"]
    assert advisory["recovery_required"] is True
    assert advisory["plan"]["plan_id"] == plan_id
    assert (
        _code(
            lambda: migration.migrate_namespace(
                repository.repo, actor="operator", expected_plan_id="0" * 64
            )
        )
        == "NAMESPACE_PLAN_DRIFT"
    )
    finished = migration.migrate_namespace(
        repository.repo, actor="operator", expected_plan_id=plan_id
    )["decision"]
    assert finished["code"] == migration.MIGRATED
    assert (
        MetadataStore(_common(repository)).load().records[record.allocation_id].record_version
        == updated["record_version"]
    )
    assert not list(current.glob(".*.tmp"))


def test_relocation_maps_only_evidence_that_existed_in_the_legacy_root(
    legacy_repository,
) -> None:
    repository = legacy_repository
    legacy = _namespace(repository, migration.LEGACY_ROOT_NAME)
    old_name = f"{uuid.uuid4()}.json"
    new_name = f"{uuid.uuid4()}.json"
    evidence.write(legacy / "completion-receipts", old_name, {"phase": "OLD"})
    plan_id = migration.plan_namespace_migration(repository.repo)["decision"]["plan"]["plan_id"]
    _crash_migration(repository, plan_id, "namespace_switched")
    current = _namespace(repository, ROOT_NAME)
    evidence.write(current / "completion-receipts", new_name, {"phase": "NEW"})
    migration.migrate_namespace(repository.repo, actor="operator", expected_plan_id=plan_id)
    mapped = relocation._record(current)["files"]
    assert f"completion-receipts/{old_name}" in mapped
    assert f"completion-receipts/{new_name}" not in mapped


def test_verify_reports_an_unsettled_prepared_receipt(legacy_repository) -> None:
    repository = legacy_repository
    plan_id = migration.plan_namespace_migration(repository.repo)["decision"]["plan"]["plan_id"]
    _crash_migration(repository, plan_id, "namespace_receipt_prepared")
    verdict = migration.verify_namespace(repository.repo)["decision"]
    assert verdict["code"] == "NAMESPACE_VERIFY_FAILED"
    assert f"PREPARED_UNSETTLED:{plan_id}" in verdict["problems"]
    assert verdict["prepared_receipts"] == [{"plan_id": plan_id, "settled": False}]


@pytest.mark.parametrize("corruption", ["identity", "manifest"])
def test_a_corrupt_prepared_receipt_fails_closed(legacy_repository, corruption: str) -> None:
    repository = legacy_repository
    plan_id = migration.plan_namespace_migration(repository.repo)["decision"]["plan"]["plan_id"]
    _crash_migration(repository, plan_id, "namespace_receipt_prepared")
    receipt = (
        _namespace(repository, ROOT_NAME) / "namespace-migrations" / f"{plan_id}.prepared.json"
    )
    document = json.loads(receipt.read_text())
    if corruption == "identity":
        document["repository_id"] = "another-repository"
    else:
        document["manifest"] = {"repo.json": "not-an-entry"}
    receipt.chmod(0o600)
    receipt.write_text(json.dumps(document))
    assert _code(lambda: migration.plan_namespace_migration(repository.repo)) == (
        "NAMESPACE_EVIDENCE_INVALID"
    )
    assert (
        _code(
            lambda: migration.migrate_namespace(
                repository.repo, actor="operator", expected_plan_id=plan_id
            )
        )
        == "NAMESPACE_EVIDENCE_INVALID"
    )


def test_receipt_listing_classifies_corrupt_documents(legacy_repository) -> None:
    repository = legacy_repository
    current = _namespace(repository, ROOT_NAME)
    directory = current / migration.RECEIPTS_DIRECTORY
    directory.mkdir(parents=True)
    (directory / "bad.json").write_text("{")
    assert _code(lambda: migration.namespace_migration_receipts(repository.repo)) == (
        "NAMESPACE_EVIDENCE_INVALID"
    )
    verdict = migration.verify_namespace(repository.repo)["decision"]
    assert verdict["code"] == "NAMESPACE_VERIFY_FAILED"
    assert "RECEIPT_UNREADABLE" in verdict["problems"]


def test_namespace_parent_symlink_is_not_a_migration_authority(legacy_repository) -> None:
    repository = legacy_repository
    current_parent = _namespace(repository, ROOT_NAME).parent
    alternative = current_parent.with_name("elsewhere")
    alternative.mkdir()
    current_parent.rmdir()
    current_parent.symlink_to(alternative, target_is_directory=True)
    assert _code(lambda: migration.plan_namespace_migration(repository.repo)) == (
        "NAMESPACE_EVIDENCE_INVALID"
    )


def test_policy_destination_parent_symlink_blocks_migration() -> None:
    with GitRepository(linked=0, policy_text=POLICY_MANUAL) as repository:
        adopt_canonical(repository)
        _leave_behind(repository, policy=True)
        destination_parent = repository.repo / ".agents"
        destination_parent.rmdir()
        external = repository.root / "external-policy"
        external.mkdir()
        destination_parent.symlink_to(external, target_is_directory=True)
        assert _code(lambda: migration.plan_namespace_migration(repository.repo)) == (
            "NAMESPACE_EVIDENCE_INVALID"
        )


def test_immutable_evidence_drift_after_switch_blocks_replay(legacy_repository) -> None:
    repository = legacy_repository
    legacy = _namespace(repository, migration.LEGACY_ROOT_NAME)
    name = f"{uuid.uuid4()}.json"
    evidence.write(legacy / "completion-receipts", name, {"phase": "OLD"})
    plan_id = migration.plan_namespace_migration(repository.repo)["decision"]["plan"]["plan_id"]
    _crash_migration(repository, plan_id, "namespace_switched")
    destination = _namespace(repository, ROOT_NAME) / "completion-receipts" / name
    destination.chmod(0o600)
    destination.write_text('{"phase":"CHANGED"}')
    assert (
        _code(
            lambda: migration.migrate_namespace(
                repository.repo, actor="operator", expected_plan_id=plan_id
            )
        )
        == "NAMESPACE_CONFLICT"
    )


@pytest.mark.parametrize("shape", ["symlink", "directory", "permission"])
def test_destination_lock_errors_have_a_controlled_code(
    legacy_repository, monkeypatch, shape: str
) -> None:
    repository = legacy_repository
    plan_id = migration.plan_namespace_migration(repository.repo)["decision"]["plan"]["plan_id"]
    current = _namespace(repository, ROOT_NAME)
    current.mkdir(parents=True)
    lock = current / "repo.lock"
    if shape == "symlink":
        lock.symlink_to(_namespace(repository, migration.LEGACY_ROOT_NAME) / "repo.lock")
    elif shape == "directory":
        lock.mkdir()
    else:
        original = migration.os.open

        def denied(path, *args, **kwargs):
            if Path(path) == lock:
                raise PermissionError(13, "permission denied", str(lock))
            return original(path, *args, **kwargs)

        monkeypatch.setattr(migration.os, "open", denied)
    assert (
        _code(
            lambda: migration.migrate_namespace(
                repository.repo, actor="operator", expected_plan_id=plan_id
            )
        )
        == "NAMESPACE_LOCK_INVALID"
    )
    assert not (current / migration.RECEIPTS_DIRECTORY).exists()


def test_relocation_refuses_a_legacy_root_that_was_not_removed(
    legacy_repository, monkeypatch
) -> None:
    repository = legacy_repository
    current = _namespace(repository, ROOT_NAME)
    plan_id = migration.plan_namespace_migration(repository.repo)["decision"]["plan"]["plan_id"]
    monkeypatch.setattr(migration, "_prune_legacy", lambda _root: False)
    assert (
        _code(
            lambda: migration.migrate_namespace(
                repository.repo, actor="operator", expected_plan_id=plan_id
            )
        )
        == "NAMESPACE_RESIDUE"
    )
    assert not (current / relocation.RECORD).exists()


def test_current_namespace_still_requires_the_exact_plan_id() -> None:
    with GitRepository(linked=0) as repository:
        adopt_canonical(repository)
        plan = migration.plan_namespace_migration(repository.repo)["decision"]
        assert plan["code"] == migration.CURRENT
        assert (
            _code(
                lambda: migration.migrate_namespace(
                    repository.repo, actor="operator", expected_plan_id="0" * 64
                )
            )
            == "NAMESPACE_PLAN_DRIFT"
        )


def test_a_crash_after_the_switch_still_publishes_relocation_evidence(
    legacy_repository,
) -> None:
    """The switch is the point of no return; finishing must not need content to move."""

    repository = legacy_repository
    legacy = _namespace(repository, migration.LEGACY_ROOT_NAME)
    transaction = str(uuid.uuid4())
    evidence.write(legacy / "completion-receipts", f"{transaction}.json", {"phase": "DONE"})
    plan_id = migration.plan_namespace_migration(repository.repo)["decision"]["plan"]["plan_id"]
    from tests.test_controlled import _cli

    assert (
        _cli(
            [
                "namespace-migrate",
                "--repo",
                str(repository.repo),
                "--actor",
                "operator",
                "--expected-plan-id",
                plan_id,
                "--json",
            ],
            crash_at="namespace_switched",
        ).returncode
        == 97
    )
    assert (_namespace(repository, ROOT_NAME) / "repo.json").is_file()
    assert not (
        _namespace(repository, ROOT_NAME) / migration.RECEIPTS_DIRECTORY / f"{plan_id}.json"
    ).exists()
    assert not (_namespace(repository, ROOT_NAME) / relocation.RECORD).exists(), (
        "relocation evidence must never precede the removal it depends on"
    )

    result = migration.migrate_namespace(
        repository.repo, actor="operator", expected_plan_id=plan_id
    )["decision"]
    assert result["code"] == migration.MIGRATED
    assert result["receipt"]["scenario"] == migration.REQUIRED
    assert (_namespace(repository, ROOT_NAME) / relocation.RECORD).is_file()
    assert not legacy.exists()


def _holder(repository: GitRepository, sentinel: Path, namespace: str) -> subprocess.Popen:
    """Park a raw holder on one namespace's repository lock until told to release."""

    ready = sentinel.with_name(sentinel.name + ".ready")
    program = "\n".join(
        (
            "import pathlib, sys, time",
            f"sys.path.insert(0, {str(SOURCE)!r})",
            "from pathlib import Path",
            "from worktree_controller.git_backend import inventory",
            "from worktree_controller.store import MetadataStore, VERSION_NAME",
            f"common = inventory(Path({str(repository.repo)!r})).common_dir",
            f"store = MetadataStore(common, _migration_root=common / {namespace!r} / VERSION_NAME)",
            "context = store.lock(exclusive=True, create=True)",
            "context.__enter__()",
            f"pathlib.Path({str(ready)!r}).write_text('held')",
            f"while not pathlib.Path({str(sentinel)!r}).exists(): time.sleep(0.02)",
            "context.__exit__(None, None, None)",
        )
    )
    return _spawn(program)


def _spawn(program: str) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-c", program],
        env={**os.environ, "PYTHONPATH": str(SOURCE), "PYTHONDONTWRITEBYTECODE": "1"},
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )


def _wait_for(path: Path, stream) -> None:
    for _ in range(400):
        if path.exists():
            return
        if stream is not None and stream.poll() is not None:
            break
        time.sleep(0.02)
    raise AssertionError("the holder never took the lock")


def test_a_migration_waits_for_the_destination_repository_lock(legacy_repository, tmp_path) -> None:
    """Nothing about the tree changes while another writer owns the destination section.

    A progress-absence assertion inside a bounded window: the migration is still blocked
    when the observation is made, and it completes once the holder releases.
    """

    repository = legacy_repository
    legacy = _namespace(repository, migration.LEGACY_ROOT_NAME)
    plan_id = migration.plan_namespace_migration(repository.repo)["decision"]["plan"]["plan_id"]
    release = tmp_path / "holder-release"
    ready = release.with_name("holder-release.ready")
    holder = _holder(repository, release, ROOT_NAME)
    arguments = [
        "--repo",
        str(repository.repo),
        "--actor",
        "operator",
        "--expected-plan-id",
        plan_id,
        "--json",
    ]
    try:
        _wait_for(ready, holder)
        pending = subprocess.Popen(
            [sys.executable, "-m", "worktree_controller.cli", "namespace-migrate", *arguments],
            env={**os.environ, "PYTHONPATH": str(SOURCE), "PYTHONDONTWRITEBYTECODE": "1"},
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        time.sleep(0.5)
        assert pending.poll() is None, "the migration did not queue behind the held lock"
        assert not (_namespace(repository, ROOT_NAME) / migration.RECEIPTS_DIRECTORY).exists(), (
            "a fenced migration wrote before it held the fence"
        )
        assert legacy.is_dir()
    finally:
        release.touch()
        holder.wait(timeout=60)
        output = pending.communicate(timeout=120)
    assert pending.returncode == 0, output[0] + output[1]
    assert json.loads(output[0])["decision"]["code"] == migration.MIGRATED


def test_a_legacy_lock_holder_fails_the_migration_closed(legacy_repository, tmp_path) -> None:
    repository = legacy_repository
    release = tmp_path / "legacy-release"
    holder = _holder(repository, release, migration.LEGACY_ROOT_NAME)
    try:
        _wait_for(release.with_name("legacy-release.ready"), holder)
        plan_id = migration.plan_namespace_migration(repository.repo)["decision"]["plan"]["plan_id"]
        assert (
            _code(
                lambda: migration.migrate_namespace(
                    repository.repo, actor="operator", expected_plan_id=plan_id
                )
            )
            == "NAMESPACE_BUSY"
        )
        assert _namespace(repository, migration.LEGACY_ROOT_NAME).is_dir()
    finally:
        release.touch()
        holder.wait(timeout=60)


def test_concurrent_migrations_settle_once(legacy_repository) -> None:
    repository = legacy_repository
    plan_id = migration.plan_namespace_migration(repository.repo)["decision"]["plan"]["plan_id"]
    arguments = [
        "--repo",
        str(repository.repo),
        "--actor",
        "operator",
        "--expected-plan-id",
        plan_id,
        "--json",
    ]
    processes = [
        subprocess.Popen(
            [sys.executable, "-m", "worktree_controller.cli", "namespace-migrate", *arguments],
            env={**os.environ, "PYTHONPATH": str(SOURCE), "PYTHONDONTWRITEBYTECODE": "1"},
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(3)
    ]
    outcomes = []
    for process in processes:
        output = process.communicate(timeout=180)
        outcomes.append((process.returncode, output))
    assert [code for code, _ in outcomes] == [0, 0, 0], outcomes
    decisions = [json.loads(output[0])["decision"] for _, output in outcomes]
    assert sum(1 for decision in decisions if decision["changed"]) == 1
    assert {decision["plan_id"] for decision in decisions} == {plan_id}
    assert not _namespace(repository, migration.LEGACY_ROOT_NAME).exists()
    directory = _namespace(repository, ROOT_NAME) / migration.RECEIPTS_DIRECTORY
    assert sorted(path.name for path in directory.glob("*.json")) == [
        f"{plan_id}.json",
        f"{plan_id}.prepared.json",
    ]


# --- verification ----------------------------------------------------------------------


def test_verify_re_derives_the_migration_from_evidence(legacy_repository) -> None:
    repository = legacy_repository
    plan_id = migration.plan_namespace_migration(repository.repo)["decision"]["plan"]["plan_id"]
    migration.migrate_namespace(repository.repo, actor="operator", expected_plan_id=plan_id)
    verdict = migration.verify_namespace(repository.repo)["decision"]
    assert verdict["code"] == "NAMESPACE_VERIFIED"
    assert verdict["relocation_record"] == "valid"
    assert verdict["legacy_root_present"] is False
    assert verdict["settled_receipts"][0]["plan_id"] == plan_id


@pytest.mark.parametrize(
    ("disturb", "problem"),
    [
        ("delete-document", "MOVED_DOCUMENT_MISSING"),
        ("legacy-reappeared", "LEGACY_NAMESPACE_PRESENT"),
        ("record-drift", "RELOCATION_RECORD"),
    ],
)
def test_verify_detects_a_disturbed_migration(
    legacy_repository, disturb: str, problem: str
) -> None:
    repository = legacy_repository
    plan_id = migration.plan_namespace_migration(repository.repo)["decision"]["plan"]["plan_id"]
    moved = migration.migrate_namespace(
        repository.repo, actor="operator", expected_plan_id=plan_id
    )["decision"]["receipt"]["moved"]
    current = _namespace(repository, ROOT_NAME)
    if disturb == "delete-document":
        (current / moved[0]).unlink()
    elif disturb == "legacy-reappeared":
        _namespace(repository, migration.LEGACY_ROOT_NAME).mkdir(parents=True)
    else:
        record = current / relocation.RECORD
        payload = json.loads(record.read_bytes())
        payload["root_identity"] = [0, 0]
        record.chmod(0o600)
        record.write_bytes((json.dumps(payload, indent=2, sort_keys=True) + "\n").encode())
    verdict = migration.verify_namespace(repository.repo)["decision"]
    assert verdict["code"] == "NAMESPACE_VERIFY_FAILED"
    assert any(item.startswith(problem) for item in verdict["problems"]), verdict["problems"]


# --- published interface ---------------------------------------------------------------


def test_the_public_cli_carries_the_migration(legacy_repository) -> None:
    repository = legacy_repository
    from tests.test_controlled import _cli

    planned = _cli(["namespace-migration-plan", "--repo", str(repository.repo), "--json"])
    assert planned.returncode == 0, planned.stderr
    decision = json.loads(planned.stdout)["decision"]
    assert decision["code"] == migration.REQUIRED

    migrated = _cli(
        [
            "namespace-migrate",
            "--repo",
            str(repository.repo),
            "--actor",
            "operator",
            "--expected-plan-id",
            decision["plan"]["plan_id"],
            "--dry-run",
            "--json",
        ]
    )
    assert migrated.returncode == 0, migrated.stderr
    assert json.loads(migrated.stdout)["decision"]["dry_run"] is True

    applied = _cli(
        [
            "namespace-migrate",
            "--repo",
            str(repository.repo),
            "--actor",
            "operator",
            "--expected-plan-id",
            decision["plan"]["plan_id"],
            "--json",
        ]
    )
    assert applied.returncode == 0, applied.stderr
    assert json.loads(applied.stdout)["decision"]["code"] == migration.MIGRATED

    listed = _cli(["namespace-migration-receipts", "--repo", str(repository.repo), "--json"])
    assert json.loads(listed.stdout)["decision"]["receipts"]
    checked = _cli(["namespace-migration-verify", "--repo", str(repository.repo), "--json"])
    assert json.loads(checked.stdout)["decision"]["code"] == "NAMESPACE_VERIFIED"
    assert _cli(["status", "--repo", str(repository.repo), "--json"]).returncode == 0


def test_the_published_migration_contract_matches_the_implementation() -> None:
    """A declared contract nobody enforces is a second, fictional interface.

    Every value the migration documents must be the value the module actually uses, so
    renaming a scenario, adding an identity field or moving a fault point fails here
    rather than quietly leaving consumers to guess.
    """

    import re

    contract = json.loads((ROOT / "tool_cli_contract.json").read_text())
    published = contract["namespace_migration_contract"]
    source = (SOURCE / "worktree_controller" / "namespace_migration.py").read_text()
    assert contract["namespace_migration_version"] == 1
    assert published["contract"] == migration.CONTRACT == "namespace-migration/v1"
    assert published["previous_namespace"] == migration.LEGACY_ROOT_NAME
    assert list(published["identity"]["fields"]) == list(migration.IDENTITY_FIELDS)
    assert list(published["fault_points"]) == list(migration.FAULT_POINTS)
    assert {
        command["name"] for command in contract["commands"] if "namespace" in command["name"]
    } == set(published["commands"])
    assert {key for key in vars(migration) if key.isupper()} >= {
        "CURRENT",
        "MIGRATED",
        "CONVERGED",
        "REQUIRED",
        "CONVERGENCE",
    }
    declared = set(published["error_codes"])
    errors = set(re.findall(r'_blocked\(\s*"(NAMESPACE_[A-Z_]+)"', source))
    # UNMANAGED is raised through its constant, so the literal scan cannot see it.
    assert errors | {migration.UNMANAGED} == declared, (
        f"published failures differ: {sorted(errors ^ declared)}"
    )
    assert '_blocked("INVALID_REQUEST"' in source, (
        "the shared refusal code stays published elsewhere"
    )
    literals = set(re.findall(r'"(NAMESPACE_[A-Z_]+)"', source))
    constants = {
        value
        for name, value in vars(migration).items()
        if name.isupper() and isinstance(value, str) and value.startswith("NAMESPACE_")
    }
    # UNMANAGED is reported as a refusal, not as a migration outcome, so it is published
    # with the failure codes rather than the result codes.
    reported = (constants | literals) - declared
    assert reported == set(published["result_codes"]), sorted(
        reported ^ set(published["result_codes"])
    )
    assert set(published["scenarios"]) == {
        migration.REQUIRED,
        migration.CURRENT,
        migration.CONVERGENCE,
        "NAMESPACE_CONFLICT",
        migration.UNMANAGED,
    }
    for code in (migration.MIGRATED, migration.CONVERGED):
        assert code in set(published["result_codes"])
    capabilities = json.loads(
        subprocess.run(
            [sys.executable, "-m", "worktree_controller.cli", "capabilities", "--json"],
            check=True,
            capture_output=True,
            text=True,
            env={**os.environ, "PYTHONPATH": str(SOURCE)},
        ).stdout
    )
    advertised = capabilities["decision"]
    assert advertised["code"] == "CAPABILITIES"
    assert advertised["contract_version"] == contract["contract_version"]
    assert advertised["namespace_migration_version"] == contract["namespace_migration_version"] == 1
    for key in ("branch_retirement_version", "branch_retirement_verify_version"):
        assert advertised[key] == contract[key], "the migration must not move retirement pins"
    assert storage_layout_pin_matches()


def storage_layout_pin_matches() -> bool:
    contract = json.loads((ROOT / "tool_cli_contract.json").read_text())
    return contract["storage_layout"]["version"] == 5 and {
        "namespace-migrations",
        "registration-prune-receipts",
    } <= set(contract["storage_layout"]["directories"])


def test_controlled_failures_surface_their_migration_code(legacy_repository) -> None:
    repository = legacy_repository
    from tests.test_controlled import _cli

    refused = _cli(
        [
            "namespace-migrate",
            "--repo",
            str(repository.repo),
            "--actor",
            "operator",
            "--expected-plan-id",
            "0" * 64,
            "--json",
        ]
    )
    assert refused.returncode != 0
    assert "NAMESPACE_PLAN_DRIFT" in refused.stdout + refused.stderr
