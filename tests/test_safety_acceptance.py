"""Current-candidate concurrency and preserved-release-restore acceptance."""

from __future__ import annotations

import json
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from tests.test_controlled import acquire_one, adopt_canonical
from tests.test_retention_admission import _record, _version
from tests.test_scope_attribution import _commit
from tests.test_scope_attribution import _preserve as preserve
from tests.test_scope_attribution import _task as new_task
from tests.test_shadow import GitRepository, git
from tests.test_writer_lease import POLICY_V6
from worktree_controller import lifecycle
from worktree_controller.controller import status
from worktree_controller.lifecycle import acquire, handoff, park, release, resume
from worktree_controller.models import ControllerError
from worktree_controller.reconciler import reconcile_writers
from worktree_controller.writer_lease import acquire_writer_lease


def test_six_concurrent_writers_three_waves():
    policy = (
        POLICY_V6.replace("normal_limit = 5", "normal_limit = 8")
        .replace("hard_limit = 9", "hard_limit = 16")
        .replace("feature_or_audit = 3", "feature_or_audit = 6")
        .replace("retained = 2", "retained = 6")
    )
    with GitRepository(linked=0, policy_text=policy) as repository:
        adopt_canonical(repository)
        for wave in range(1, 4):
            barrier = threading.Barrier(6)

            def start_writer(index, wave=wave, barrier=barrier):
                task = f"wave-{wave}-writer-{index}"
                barrier.wait(timeout=30)
                path, payload = acquire_one(
                    repository,
                    task=task,
                    authority=task,
                    owner="independent-review",
                    scope_paths=("work.txt",),
                )
                allocation = payload["decision"]["allocation_id"]
                return path, allocation

            with ThreadPoolExecutor(max_workers=6) as pool:
                writers = list(pool.map(start_writer, range(6)))
            children = [
                subprocess.Popen(["sleep", "240"], start_new_session=True) for _ in range(6)
            ]
            try:

                def bind_writer(index, writers=writers, children=children):
                    path, _ = writers[index]
                    decision = acquire_writer_lease(
                        repository.repo,
                        target=path,
                        client_id="manual",
                        holder_pid=children[index].pid,
                    )["decision"]
                    assert decision["code"] == "WRITER_LEASE_ACQUIRED"

                with ThreadPoolExecutor(max_workers=6) as pool:
                    list(pool.map(bind_writer, range(6)))
                current = status(repository.repo)
                assert (
                    sum(
                        w["lifecycle"] == "ACTIVE" and w["role"] == "feature"
                        for w in current["worktrees"]
                    )
                    == 6
                )
                for index, (path, _) in enumerate(writers):
                    _commit(path, "work.txt", f"synthetic wave {wave} writer {index}", "test work")
                from worktree_controller.controller import plan

                decision = plan(repository.repo, task_key=f"overflow-{wave}",
                                authority_key=f"overflow-{wave}", module="m", role="feature")
                assert decision["decision"]["code"] == "CREATE_ALLOWED"
            finally:
                for child in children:
                    child.terminate()
                    child.wait()
            # Deliberately omit terminal notification and writer-lease-release.
            result = reconcile_writers(repository.repo, actor="lifecycle-reconciler", dry_run=False)
            assert result["decision"]["reclaimed"] == 6
            assert status(repository.repo)["repository"]["retained_used"] == 6
            heads = {}
            for path, allocation in writers:
                assert _record(repository, allocation).lifecycle == "RETAINED"
                head = git(path, "rev-parse", "HEAD")
                branch = git(path, "branch", "--show-current")
                heads[branch] = head
                handoff(
                    repository.repo,
                    target=path,
                    expected_record_version=_version(repository, allocation),
                    kind="preserved",
                    commit=head,
                    finalizer_commit=head,
                    finalizer_record_id=f"synthetic-test:{head}",
                )
                assert _record(repository, allocation).lifecycle == "PRESERVED"
                release(
                    repository.repo,
                    target=path,
                    expected_record_version=_version(repository, allocation),
                    owner_release="independent-review",
                )
                assert not path.exists()
                assert _record(repository, allocation).lifecycle == "RELEASED"
            final = status(repository.repo)
            assert final["repository"]["worktree_count"] == 1
            assert final["repository"]["retained_used"] == 0
            for branch, head in heads.items():
                assert git(repository.repo, "rev-parse", branch) == head
            print(
                json.dumps(
                    {
                        "wave": wave,
                        "concurrent_live_writers": 6,
                        "lost_notifications": 6,
                        "reclaimed_to_retained": 6,
                        "preserved_and_released": 6,
                        "retained_headroom": 6,
                        "branches_retained": 6,
                    }
                ),
                flush=True,
            )


def test_acquire_resume_reconcile_race():
    with GitRepository(linked=0, policy_text=POLICY_V6) as repository:
        adopt_canonical(repository)
        path, payload = acquire_one(
            repository, task="resume", authority="resume", owner="review-owner"
        )
        allocation = payload["decision"]["allocation_id"]
        park(
            repository.repo,
            target=path,
            expected_record_version=_version(repository, allocation),
            owner="review-owner",
        )
        for index in range(2):
            acquire_one(repository, task=f"active-{index}", authority=f"active-{index}")
        dead_path, _ = acquire_one(repository, task="dead", authority="dead")
        child = subprocess.Popen(["sleep", "60"], start_new_session=True)
        try:
            acquire_writer_lease(
                repository.repo, target=dead_path, client_id="manual", holder_pid=child.pid
            )
        finally:
            child.terminate()
            child.wait()
        expected_version = _version(repository, allocation)
        barrier = threading.Barrier(3)

        def race(kind):
            barrier.wait(timeout=30)
            try:
                if kind == "resume":
                    result = resume(
                        repository.repo,
                        target=path,
                        expected_record_version=expected_version,
                        owner="review-owner",
                    )
                elif kind == "acquire":
                    _, result = acquire_one(repository, task="new", authority="new")
                else:
                    result = reconcile_writers(
                        repository.repo, actor="lifecycle-reconciler", dry_run=False
                    )
                return kind, result["decision"]["code"]
            except ControllerError as error:
                assert error.code == "CAS_MISMATCH"
                return kind, error.code

        with ThreadPoolExecutor(max_workers=3) as pool:
            results = list(pool.map(race, ["resume", "acquire", "reconcile"]))
        current = status(repository.repo)
        assert (
            sum(w["lifecycle"] == "ACTIVE" and w["role"] == "feature" for w in current["worktrees"])
            == 4
        )
        assert current["repository"]["retained_used"] <= 2
        print(json.dumps({"race": results}), flush=True)


@pytest.mark.parametrize("blocker", ["scope", "owner", "cas", "binding"])
def test_preservation_gates(blocker):
    with GitRepository(linked=0, policy_text=POLICY_V6) as repository:
        adopt_canonical(repository)
        path, allocation, _ = new_task(repository)
        if blocker == "binding":
            lifecycle.update_external_binding(
                repository.repo,
                target=path,
                expected_record_version=_version(repository, allocation),
                owner="review-owner",
                binding="synthetic-runtime",
                action="add",
            )
        head = _commit(path, "uv.lock" if blocker == "scope" else "mine.txt", "work", "work")
        if blocker == "scope":
            with pytest.raises(ControllerError) as error:
                preserve(repository, path, allocation, head)
            assert error.value.code == "PATH_SCOPE_VIOLATION"
            return
        preserve(repository, path, allocation, head)
        with pytest.raises(ControllerError) as error:
            release(
                repository.repo,
                target=path,
                expected_record_version=0 if blocker == "cas" else _version(repository, allocation),
                owner_release="wrong-owner" if blocker == "owner" else "review-owner",
            )
        assert (
            error.value.code
            == {"owner": "ACTIVE_OWNER", "cas": "CAS_MISMATCH", "binding": "EXTERNAL_BINDING"}[
                blocker
            ]
        )
        assert path.exists()


def test_preserved_release_restore_keeps_task_and_retained_ref():
    with GitRepository(linked=0, policy_text=POLICY_V6) as repository:
        adopt_canonical(repository)
        path, allocation, record = new_task(repository)
        head = _commit(path, "mine.txt", "kept", "preserve me")
        assert preserve(repository, path, allocation, head)["decision"]["code"] == "PRESERVED"
        assert (
            release(
                repository.repo,
                target=path,
                expected_record_version=_version(repository, allocation),
                owner_release="review-owner",
            )["decision"]["code"]
            == "RELEASED"
        )
        retained = _record(repository, allocation)
        assert retained.lifecycle == "RELEASED"
        assert retained.task_key == record.task_key
        assert retained.handoff_commit == head
        assert retained.finalizer_record_id == f"synthetic-test-only:{head}"
        assert git(repository.repo, "rev-parse", "feature/mine") == head
        assert not path.exists()
        restored_path = repository.root / "mine-restored"
        restored = acquire(
            repository.repo,
            task_key=retained.task_key,
            authority_key=retained.authority_key,
            module=retained.module,
            role=retained.role,
            owner=retained.owner,
            branch="feature/mine-restored",
            base_ref=head,
            target=restored_path,
            scope_paths=retained.scope_paths,
        )
        assert restored["decision"]["code"] == "ACQUIRED"
        assert git(restored_path, "rev-parse", "HEAD") == head
        assert (restored_path / "mine.txt").read_text() == "kept"
        assert _record(repository, allocation) == retained
        assert _record(repository, restored["decision"]["allocation_id"]).owner == retained.owner
