"""Diagnostics must keep inspected checkouts alive through semantic reads."""

import fcntl
import json
import multiprocessing
import os
from dataclasses import replace

import pytest

from tests.test_controlled import _cli, acquire_one, adopt_canonical, handoff_one
from tests.test_shadow import POLICY_V2, GitRepository, git
from worktree_controller import controller
from worktree_controller.git_backend import inventory
from worktree_controller.lifecycle import release
from worktree_controller.models import ControllerError
from worktree_controller.store import MetadataStore


def _release_after_lock_probe(repo, target, version, connection):
    try:
        store = MetadataStore(inventory(repo).common_dir)
        descriptor = os.open(store.lock_file, os.O_RDONLY)
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                blocked = True
            else:
                blocked = False
                fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)
        connection.send({"lock_blocked": blocked})
        result = release(
            repo, target=target, expected_record_version=version, owner_release="module-owner"
        )
        connection.send({"code": result["decision"]["code"]})
    except Exception as error:
        connection.send({"error": repr(error)})
    finally:
        connection.close()


def _receive(connection):
    assert connection.poll(30), "release process did not reach its next checkpoint"
    return connection.recv()


def _handed_off(repository):
    adopt_canonical(repository)
    target, _ = acquire_one(repository, scope_paths=("result.txt",))
    (target / "result.txt").write_text("preserved result\n")
    git(target, "add", "result.txt")
    git(target, "commit", "-m", "fixture result")
    decision = handoff_one(repository, target, scope_paths=("result.txt",))["decision"]
    return target, decision


def test_doctor_serializes_semantic_reads_with_guarded_release(monkeypatch):
    with GitRepository(linked=0, policy_text=POLICY_V2) as repository:
        target, handed = _handed_off(repository)
        head = git(target, "rev-parse", "HEAD")
        context = multiprocessing.get_context("spawn")
        receiver, sender = context.Pipe(duplex=False)
        process = context.Process(
            target=_release_after_lock_probe,
            args=(repository.repo, target, handed["record_version"], sender),
        )
        original = controller._semantic_warnings
        observations = []
        released = []

        def release_during_semantics(*arguments):
            process.start()
            sender.close()
            observation = _receive(receiver)
            observations.append(observation)
            if not observation["lock_blocked"]:
                # Reproduce the old interleaving without a timing-dependent sleep:
                # the retained Git object exists, but its checkout has disappeared.
                released.append(_receive(receiver))
            return original(*arguments)

        monkeypatch.setattr(controller, "_semantic_warnings", release_during_semantics)
        try:
            result = controller.doctor(repository.repo)
            if not released:
                released.append(_receive(receiver))
            process.join(30)
            assert process.exitcode == 0
            assert observations == [{"lock_blocked": True}]
            assert result["decision"]["code"] == "PASS"
            assert released == [{"code": "RELEASED"}]
            assert not target.exists()
            assert git(repository.repo, "rev-parse", "feature/new-task") == head
            assert git(repository.repo, "show", f"{head}:result.txt") == "preserved result"
        finally:
            if process.pid is not None:
                if process.is_alive():
                    process.terminate()
                process.join(30)
            sender.close()
            receiver.close()


def test_doctor_refreshes_inventory_after_taking_its_read_lock(monkeypatch):
    with GitRepository(linked=0, policy_text=POLICY_V2) as repository:
        target, handed = _handed_off(repository)
        original = controller.diagnose_mounts

        def release_before_lock(*arguments, **keywords):
            diagnostics = original(*arguments, **keywords)
            monkeypatch.setattr(controller, "diagnose_mounts", original)
            released = release(
                repository.repo,
                target=target,
                expected_record_version=handed["record_version"],
                owner_release="module-owner",
            )
            assert released["decision"]["code"] == "RELEASED"
            return diagnostics

        monkeypatch.setattr(controller, "diagnose_mounts", release_before_lock)
        result = controller.doctor(repository.repo)
        assert result["decision"]["code"] == "PASS"
        assert all(row["path"] != str(target) for row in result["worktrees"])


def test_doctor_still_refuses_a_genuinely_missing_commit():
    with GitRepository(linked=0, policy_text=POLICY_V2) as repository:
        target, handed = _handed_off(repository)
        store = MetadataStore(inventory(repository.repo).common_dir)
        record = store.load().records[handed["allocation_id"]]
        store.replace_record(replace(record, base_commit="f" * 40))
        with pytest.raises(ControllerError) as refused:
            controller.doctor(repository.repo)
        assert refused.value.code == "INVALID_COMMIT"
        cli = _cli(["doctor", "--repo", str(repository.repo), "--json"])
        assert cli.returncode == 2
        assert json.loads(cli.stdout)["errors"][0]["code"] == "INVALID_COMMIT"
        assert target.exists()
        assert git(target, "show", "HEAD:result.txt") == "preserved result"
