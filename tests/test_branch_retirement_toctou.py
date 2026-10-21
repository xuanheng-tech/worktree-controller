"""The verify→mutation window, and what closes it.

1.6.0 released a verdict computed under a shared lock, let the consumer mutate a
ref outside any Controller section, and then turned the consumer's own JSON into a
governance receipt.  ``git update-ref -d`` has no occupancy guard, so the window
really could orphan a live checkout and still be filed as governed completion.

Each test here is written so that removing the corresponding guard turns it red.
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
from worktree_controller.branch_retirement import (
    authorize_branch_retirement,
    branch_retirement_status,
    complete_branch_retirement,
    plan_branch_retirement,
    record_branch_retirement,
    verify_branch_retirement,
)
from worktree_controller.models import ControllerError

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "src"
CLI = [sys.executable, "-m", "worktree_controller.cli"]


def _planned(repository: GitRepository, name: str) -> dict:
    adopt_canonical(repository)
    _configure_remote(repository)
    allocation, _head = _released_source(repository, name)
    payload = plan_branch_retirement(
        repository.repo,
        allocation_id=allocation,
        remote="origin",
        integrated_into="main",
    )["decision"]
    assert payload["code"] == "BRANCH_RETIREMENT_READY"
    return payload["plan"]


def _summary(repository: GitRepository, plan: dict, path: Path) -> Path:
    path.write_text(
        json.dumps(
            {
                "finalizer_version": "mock/1",
                "mode": "retire_local_branch",
                "status": "success",
                "mode_result": {
                    "retirement_plan_id": plan["plan_id"],
                    "result": "LOCAL_BRANCH_RETIRED_VERIFIED",
                    "branch": plan["branch"],
                    "remote": plan["remote"],
                    "expected_oid": plan["expected_oid"],
                    "integrated_into_oid": plan["expected_integrated_oid"],
                },
            }
        ),
        encoding="utf-8",
    )
    return path


def _receipt_files(repository: GitRepository) -> list[str]:
    directory = (
        repository.repo / ".git" / "worktree-controller" / "v1" / "branch-retirement-receipts"
    )
    return sorted(path.name for path in directory.glob("*.json")) if directory.is_dir() else []


def _ref(repository: GitRepository, ref: str) -> str | None:
    result = subprocess.run(
        ["git", "-C", str(repository.repo), "rev-parse", "--verify", "-q", ref],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip() or None


def test_unobserved_claim_cannot_file_a_receipt(tmp_path: Path) -> None:
    """T-1: the released sink - a well-shaped summary with no mutation behind it."""

    with GitRepository(linked=0, policy_text=POLICY_MANUAL) as repository:
        plan = _planned(repository, "toctou")
        with pytest.raises(ControllerError) as refused:
            record_branch_retirement(
                repository.repo,
                plan_id=plan["plan_id"],
                operation="local",
                summary_path=_summary(repository, plan, tmp_path / "summary.json"),
            )
        assert refused.value.code == "EXECUTION_OBSERVATION_MISMATCH"
        assert _receipt_files(repository) == []
        assert (
            verify_branch_retirement(repository.repo, plan_id=plan["plan_id"], operation="local")[
                "decision"
            ]["code"]
            == "RETIREMENT_EXECUTABLE"
        )


def test_released_window_no_longer_launders_an_orphaned_checkout(tmp_path: Path) -> None:
    """T-2: verify, hijack the branch with plain git, CAS, then try to file it."""

    with GitRepository(linked=0, policy_text=POLICY_MANUAL) as repository:
        plan = _planned(repository, "toctou")
        verdict = verify_branch_retirement(
            repository.repo, plan_id=plan["plan_id"], operation="local"
        )["decision"]
        assert verdict["code"] == "RETIREMENT_EXECUTABLE"
        fields = verdict["minimum_revalidation"]
        assert set(fields) == {
            "plan_id",
            "operation",
            "branch",
            "expected_oid",
            "expected_integrated_oid",
            "integrated_ref",
            "ref_state",
            "controller_state_digest",
        }

        # No Controller file is touched and no lock is taken: only plain git.
        checkout = repository.root / "hijacked"
        git(repository.repo, "worktree", "add", str(checkout), plan["branch"])
        deleted = subprocess.run(
            [
                "git",
                "-C",
                str(repository.repo),
                "update-ref",
                "-d",
                f"refs/heads/{plan['branch']}",
                fields["expected_oid"],
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        assert deleted.returncode == 0, deleted.stderr
        assert _ref(repository, f"refs/heads/{plan['branch']}") is None
        # git's own worktree guard does not apply to update-ref, so the worktree
        # is now dangling at a branch that no longer exists.
        assert plan["branch"] in git(repository.repo, "worktree", "list")

        with pytest.raises(ControllerError) as refused:
            record_branch_retirement(
                repository.repo,
                plan_id=plan["plan_id"],
                operation="local",
                summary_path=_summary(repository, plan, tmp_path / "summary.json"),
            )
        assert refused.value.code == "EXECUTION_GOVERNANCE_MOVED"
        assert "BRANCH_CHECKED_OUT" in str(refused.value)
        assert _receipt_files(repository) == []


def test_grant_refuses_the_same_interleaving_with_its_own_observation(tmp_path: Path) -> None:
    """T-3: an authorized run settles from what the Controller re-reads, not a claim."""

    with GitRepository(linked=0, policy_text=POLICY_MANUAL) as repository:
        plan = _planned(repository, "toctou")
        grant = authorize_branch_retirement(
            repository.repo,
            plan_id=plan["plan_id"],
            operation="local",
            request_id=str(uuid.uuid4()),
        )["decision"]["authorization"]
        checkout = repository.root / "late"
        git(repository.repo, "worktree", "add", str(checkout), plan["branch"])
        assert (
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(repository.repo),
                    "update-ref",
                    "-d",
                    grant["ref"],
                    grant["expected_oid"],
                ],
                check=False,
            ).returncode
            == 0
        )
        result = tmp_path / "result.json"
        result.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "contract": "retirement-execution-result/v1",
                    "plan_id": grant["plan_id"],
                    "operation": grant["operation"],
                    "kind": grant["kind"],
                    "outcome": "REF_REMOVED_AT_EXPECTED_OID",
                    "ref": grant["ref"],
                    "expected_oid": grant["expected_oid"],
                    "expected_integrated_oid": grant["expected_integrated_oid"],
                    "performed_at": "2026-09-25T00:00:00+08:00",
                    "executor": {"name": "mock-retirement-consumer", "version": "1"},
                }
            ),
            encoding="utf-8",
        )
        settled = complete_branch_retirement(
            repository.repo,
            authorization_id=grant["authorization_id"],
            token=grant["token"],
            result_path=result,
        )["decision"]
        assert settled["code"] == "RETIREMENT_EXECUTION_REFUSED"
        assert "OBSERVATION_CHECKOUT_APPEARED" in settled["blockers"]
        assert _receipt_files(repository) == []
        assert Path(settled["evidence"]["path"]).is_file()


def test_grant_is_minted_only_inside_the_controllers_own_section(tmp_path: Path) -> None:
    """P1: no grant document exists while another writer holds the repository section.

    A progress-absence observation inside a bounded window, not a timing race: the
    authorize process is provably still blocked, and it completes once the holder
    releases.  ``MetadataStore.lock`` always opens a fresh descriptor, so a second
    exclusive request cannot succeed against a held section.
    """

    with GitRepository(linked=0, policy_text=POLICY_MANUAL) as repository:
        plan = _planned(repository, "toctou")
        ready = tmp_path / "holder-ready"
        release = tmp_path / "holder-release"
        holder_log = (tmp_path / "holder.log").open("w+b")
        holder = subprocess.Popen(
            [
                sys.executable,
                "-c",
                (
                    "\n".join(
                        (
                            f"import sys, time, pathlib; sys.path.insert(0, {str(SOURCE)!r})",
                            "from pathlib import Path",
                            "from worktree_controller.store import MetadataStore",
                            "from worktree_controller.git_backend import inventory",
                            f"store = MetadataStore(inventory(Path({str(repository.repo)!r})).common_dir)",
                            "context = store.lock(exclusive=True, create=True)",
                            "context.__enter__()",
                            f"pathlib.Path({str(ready)!r}).write_text('held')",
                            f"while not pathlib.Path({str(release)!r}).exists(): time.sleep(0.05)",
                            "context.__exit__(None, None, None)",
                        )
                    )
                    + "\n"
                ),
            ],
            env={**os.environ, "PYTHONPATH": str(SOURCE), "PYTHONDONTWRITEBYTECODE": "1"},
            stdout=holder_log,
            stderr=subprocess.STDOUT,
        )
        authorizations = (
            repository.repo
            / ".git"
            / "worktree-controller"
            / "v1"
            / "branch-retirement-authorizations"
        )
        blocked = None
        try:
            for _ in range(400):
                if ready.exists() or holder.poll() is not None:
                    break
                time.sleep(0.05)
            assert ready.exists(), _holder_output(holder_log, holder)
            blocked = subprocess.Popen(
                [
                    *CLI,
                    "branch-retirement-authorize",
                    "--repo",
                    str(repository.repo),
                    "--plan-id",
                    plan["plan_id"],
                    "--operation",
                    "local",
                    "--request-id",
                    str(uuid.uuid4()),
                    "--json",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env={**os.environ, "PYTHONPATH": str(SOURCE), "PYTHONDONTWRITEBYTECODE": "1"},
            )
            time.sleep(1.5)
            assert blocked.poll() is None, "authorize wrote a grant inside a held section"
            assert not authorizations.exists() or list(authorizations.glob("*.json")) == []
            release.write_text("release", encoding="utf-8")
            output, error = blocked.communicate(timeout=180)
            assert blocked.returncode == 0, error
            assert json.loads(output)["decision"]["code"] == "RETIREMENT_AUTHORIZED"
        finally:
            release.write_text("release", encoding="utf-8")
            holder.wait(timeout=120)
            if blocked is not None and blocked.poll() is None:
                blocked.kill()
                blocked.wait(timeout=30)
            holder_log.close()
        assert len(list(authorizations.glob("*.json"))) == 1


def _holder_output(log, holder: subprocess.Popen) -> str:
    log.flush()
    log.seek(0)
    return f"holder rc={holder.poll()} output: {log.read().decode(errors='replace')[-700:]}"


def test_a_local_grant_does_not_disturb_the_remote_half_or_released_payloads(
    tmp_path: Path,
) -> None:
    """The outstanding-grant signal is per operation, at the verb layer only."""

    with GitRepository(linked=0, policy_text=POLICY_MANUAL) as repository:
        plan = _planned(repository, "toctou")
        granted = authorize_branch_retirement(
            repository.repo,
            plan_id=plan["plan_id"],
            operation="local",
            request_id=str(uuid.uuid4()),
        )["decision"]
        local = verify_branch_retirement(
            repository.repo, plan_id=plan["plan_id"], operation="local"
        )["decision"]
        assert "RETIREMENT_AUTHORIZATION_ACTIVE" in local["blockers"]
        remote = verify_branch_retirement(
            repository.repo, plan_id=plan["plan_id"], operation="remote"
        )["decision"]
        assert "RETIREMENT_AUTHORIZATION_ACTIVE" not in remote["blockers"]
        assert set(remote) == {
            "code",
            "plan_id",
            "operation",
            "blockers",
            "receipt",
            "minimum_revalidation",
        }
        status = branch_retirement_status(repository.repo, plan_id=plan["plan_id"])["decision"]
        assert status["code"] == "BRANCH_RETIREMENT_READY"
        assert set(status) == {
            "code",
            "plan_id",
            "completed_operations",
            "pending_operations",
            "receipts",
            "current",
        }
        assert granted["authorization"]["observed"]["checked_out_paths"] == []


def test_holder_naming_a_foreign_process_is_refused(tmp_path: Path) -> None:
    with GitRepository(linked=0, policy_text=POLICY_MANUAL) as repository:
        plan = _planned(repository, "toctou")
        with pytest.raises(ControllerError) as refused:
            authorize_branch_retirement(
                repository.repo,
                plan_id=plan["plan_id"],
                operation="local",
                request_id=str(uuid.uuid4()),
                holder=str(os.getpid()),
            )
        assert refused.value.code == "INVALID_HOLDER"


def test_attest_absent_is_not_a_grantable_kind() -> None:
    with GitRepository(linked=0, policy_text=POLICY_MANUAL) as repository:
        plan = _planned(repository, "toctou")
        with pytest.raises(ControllerError) as refused:
            authorize_branch_retirement(
                repository.repo,
                plan_id=plan["plan_id"],
                operation="local",
                request_id=str(uuid.uuid4()),
                kind="ATTEST_ABSENT",
            )
        assert refused.value.code == "INVALID_EXECUTION_KIND"


def test_a_live_grant_refuses_rebinding_the_granted_allocation() -> None:
    """The fence holds at the one chokepoint every new occupancy passes through."""

    from dataclasses import replace

    from worktree_controller.git_backend import inventory as git_inventory
    from worktree_controller.store import MetadataStore

    with GitRepository(linked=0, policy_text=POLICY_MANUAL) as repository:
        plan = _planned(repository, "fence")
        authorize_branch_retirement(
            repository.repo,
            plan_id=plan["plan_id"],
            operation="local",
            request_id=str(uuid.uuid4()),
        )
        store = MetadataStore(git_inventory(repository.repo).common_dir)
        record = store.load().records[str(plan["allocation_id"])]
        fresh_key = "wt_" + "0" * 64
        # load() takes its own lock, so the record above is read before entering the
        # section: a nested store.lock would block on the flock this one already holds.
        with store.lock(exclusive=True), pytest.raises(ControllerError) as refused:
            store.write_record(fresh_key, replace(record))
        assert refused.value.code == "RETIREMENT_EXECUTION_ACTIVE"
        assert (store.branch_retirement_plans_dir / f"{plan['plan_id']}.json").is_file(), (
            "the refusal must not disturb the plan or the grant"
        )


def _methods_touching(needle: str) -> set[str]:
    from inspect import getsource

    from worktree_controller.store import MetadataStore

    found: set[str] = set()
    for name in dir(MetadataStore):
        attribute = getattr(MetadataStore, name)
        if not callable(attribute):
            continue
        try:
            source = getsource(attribute)
        except (OSError, TypeError):
            continue
        if needle in source:
            found.add(name)
    return found


def test_the_occupancy_chokepoint_stays_the_only_binding_writer() -> None:
    """If a second writer appears, or the guard is dropped, this fails by default."""

    from inspect import getsource

    from worktree_controller.store import MetadataStore

    binding_path = "bindings_dir" + ' / f"{worktree_key}.json"'
    assert _methods_touching(binding_path) == {
        "write_record",
        "remove_binding",
        "_methods_touching",
    } - {"_methods_touching"}, "a new binding writer bypasses the fence"
    guarded = getsource(MetadataStore.write_record)
    assert "retirement_occupancy_blocker" in guarded
    assert "if not (self.bindings_dir" in guarded, "the guard must key on new occupancy only"
