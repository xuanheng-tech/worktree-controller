"""A host session keeps its own frozen handoff's Controller flow, never its fence."""

from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
from threading import Event, Thread

import pytest

from tests.test_controlled import _configure_remote, acquire_one, adopt_canonical, handoff_one
from tests.test_host_managed_writer_session import hosted as hosted
from tests.test_host_managed_writer_session import metadata
from tests.test_integration_pipeline import POLICY_THREE_CANDIDATES, _claim, _enqueue
from tests.test_retention_admission import _store
from tests.test_shadow import GitRepository, git
from tests.test_write_admission import task as task
from worktree_controller import controller, writer_lease, writer_session
from worktree_controller.cli import _main as cached_controller_main
from worktree_controller.cli import main as controller_main
from worktree_controller.store import decode_cache


@pytest.fixture
def frozen(monkeypatch):
    with GitRepository(linked=0, policy_text=POLICY_THREE_CANDIDATES) as repository:
        adopt_canonical(repository)
        _configure_remote(repository)
        path, payload = acquire_one(
            repository, task="hosted", authority="hosted", scope_paths=("hosted.txt",)
        )
        allocation = str(payload["decision"]["allocation_id"])
        (path / "hosted.txt").write_text("hosted\n", encoding="utf-8")
        git(path, "add", "hosted.txt")
        git(path, "commit", "-m", "add hosted")
        holder = writer_lease.resolve_holder(
            str(os.getpid()), client_id="host-application", session_id="host-session", run_id=None
        )
        lease = writer_lease.acquire_writer_lease(
            repository.repo,
            target=path,
            client_id=holder.client_id,
            holder_pid=holder.pid,
            session_id=holder.session_id,
            run_id=None,
            holder=holder,
        )["decision"]
        context = {
            "holder": {"pid": holder.pid, "boot_id": holder.boot_id, "start_ticks": holder.start_ticks},
            "client_id": holder.client_id,
            "session_id": holder.session_id,
            "run_id": None,
            "repository_path": str(repository.repo),
            "worktree_path": str(path),
            "allocation_id": allocation,
            "lease_id": lease["lease_id"],
        }
        monkeypatch.setenv("CONTROLLER_WRITER_MODE", "host-managed-v2")
        monkeypatch.setenv("CONTROLLER_WRITER_CONTEXT", json.dumps(context))
        assert writer_session.host_check(path)["code"] == "WRITE_ALLOWED"
        handoff_one(repository, path)
        yield repository, path, allocation, holder


def test_handoff_keeps_the_feature_write_fence_for_unclassified_checks(frozen):
    repository, path, _, _ = frozen
    before = metadata(repository)
    decision = writer_session.host_check(path)
    assert decision["admission"] == "REFUSED"
    assert decision["code"] == "BLOCK_FEATURE_WRITE_LIFECYCLE"
    assert metadata(repository) == before


@pytest.mark.parametrize("access", ["read", "exec"])
def test_same_holder_reads_and_runs_commands_after_its_own_handoff(frozen, access):
    repository, path, allocation, _ = frozen
    before = metadata(repository)
    decision = writer_session.host_check(path, access=access)
    assert decision["admission"] == "CONTINUATION_GRANTED"
    assert decision["code"] == "HOST_HANDOFF_CONTINUATION"
    assert decision["allocation_id"] == allocation and decision["lifecycle"] == "HANDOFF_READY"
    assert metadata(repository) == before


@pytest.mark.parametrize("relative", ["hosted.txt", "new/file.txt", "."])
def test_frozen_checkout_still_refuses_writes(frozen, relative):
    repository, path, _, _ = frozen
    before = metadata(repository)
    decision = writer_session.host_check(path, access="write", target=path / relative)
    assert decision["admission"] == "REFUSED"
    assert decision["code"] == "BLOCK_FEATURE_WRITE_LIFECYCLE"
    assert metadata(repository) == before


def test_symlink_into_the_frozen_checkout_is_refused(frozen, tmp_path):
    _, path, _, _ = frozen
    link = tmp_path / "alias"
    link.symlink_to(path / "hosted.txt")
    decision = writer_session.host_check(path, access="write", target=link)
    assert decision["code"] == "BLOCK_FEATURE_WRITE_LIFECYCLE"


def test_writes_outside_governance_continue(frozen, tmp_path):
    _, path, _, _ = frozen
    decision = writer_session.host_check(path, access="write", target=tmp_path / "notes" / "x.md")
    assert decision["admission"] == "CONTINUATION_GRANTED"


def test_other_governed_checkouts_stay_refused(frozen):
    repository, path, _, _ = frozen
    other, _ = acquire_one(repository, task="other", authority="other", scope_paths=("other.txt",))
    for target in (other / "other.txt", repository.repo / "canonical.txt"):
        decision = writer_session.host_check(path, access="write", target=target)
        assert decision["admission"] == "REFUSED", target


def test_enqueue_claim_and_candidate_writes_continue_after_handoff(frozen):
    repository, path, allocation, _ = frozen
    queued = _enqueue(repository, task="hosted-integration", source_allocation=allocation)
    assert writer_session.host_check(path, access="exec")["admission"] == "CONTINUATION_GRANTED"
    candidate, claimed = _claim(
        repository, task="hosted-integration", intent_id=str(queued["decision"]["intent_id"])
    )
    assert claimed["decision"]["code"] == "INTEGRATION_CANDIDATE_CLAIMED"
    decision = writer_session.host_check(path, access="write", target=candidate / "hosted.txt")
    assert decision["admission"] == "CONTINUATION_GRANTED"
    assert decision["candidate_allocation_id"] != allocation
    # The source checkout stays frozen while its candidate is open.
    assert writer_session.host_check(path, access="write", target=path / "hosted.txt")[
        "admission"
    ] == "REFUSED"


def test_candidate_of_another_source_is_refused(frozen):
    repository, path, _, _ = frozen
    other, payload = acquire_one(repository, task="other", authority="other", scope_paths=("other.txt",))
    (other / "other.txt").write_text("other\n", encoding="utf-8")
    git(other, "add", "other.txt")
    git(other, "commit", "-m", "add other")
    handoff_one(repository, other)
    queued = _enqueue(
        repository, task="other-integration", source_allocation=str(payload["decision"]["allocation_id"])
    )
    candidate, _ = _claim(
        repository, task="other-integration", intent_id=str(queued["decision"]["intent_id"])
    )
    decision = writer_session.host_check(path, access="write", target=candidate / "other.txt")
    assert decision["admission"] == "REFUSED"
    assert decision["code"] == "BLOCK_HOST_CONTINUATION_TARGET"


def test_released_lease_or_foreign_holder_refuses_even_reads(frozen, monkeypatch):
    repository, path, _, holder = frozen
    context = json.loads(os.environ["CONTROLLER_WRITER_CONTEXT"])
    monkeypatch.setenv(
        "CONTROLLER_WRITER_CONTEXT", json.dumps({**context, "session_id": "another-session"})
    )
    assert writer_session.host_check(path, access="read")["admission"] == "REFUSED"
    monkeypatch.setenv("CONTROLLER_WRITER_CONTEXT", json.dumps(context))
    writer_lease.release_writer_lease(repository.repo, target=path, holder=holder)
    decision = writer_session.host_check(path, access="read")
    assert decision["admission"] == "REFUSED"
    assert decision["code"] == "WRITER_LEASE_REQUIRED"


def test_frozen_head_drift_refuses_continuation(frozen):
    _, path, _, _ = frozen
    git(path, "commit", "--allow-empty", "-m", "drift after handoff")
    decision = writer_session.host_check(path, access="read")
    assert decision["admission"] == "REFUSED"
    assert decision["code"] == "RECOVERY_REQUIRED"


@pytest.mark.parametrize(
    "access,target",
    [("write", None), ("read", Path("/tmp")), ("delete", None), ("write", Path("relative.txt"))],
)
def test_malformed_requests_fail_closed(frozen, access, target):
    _, path, _, _ = frozen
    assert writer_session.host_check(path, access=access, target=target)["admission"] == "REFUSED"


@pytest.mark.parametrize("access", [None, "read", "exec", "write"])
def test_active_checkout_keeps_its_existing_grant(hosted, tmp_path, access):
    _, path, _, _, _ = hosted
    target = tmp_path / "anywhere" if access == "write" else None
    decision = writer_session.host_check(path, access=access, target=target)
    assert decision["admission"] == "GRANTED" and decision["code"] == "WRITE_ALLOWED"


def test_cli_exit_status_follows_the_admission(frozen, capsys):
    _, path, _, _ = frozen
    assert controller_main(["writer-session", "host-check", str(path), "--access", "read"]) == 0
    assert json.loads(capsys.readouterr().out)["decision"]["admission"] == "CONTINUATION_GRANTED"
    assert controller_main(["writer-session", "host-check", str(path)]) == 2
    capsys.readouterr()
    assert controller_main(
        ["writer-session", "host-check", str(path), "--access", "write", "--target", str(path / "x")]
    ) == 2


@pytest.mark.parametrize("mode", ("check", "host-check"))
@pytest.mark.parametrize("corruption", (None, "crlf-json", "utf8", "schema"))
def test_writer_cli_preserves_cached_reader_business_payload(frozen, capsys, mode, corruption):
    repository, path, allocation, holder = frozen
    if corruption is not None:
        record = _store(repository).records_dir / f"{allocation}.json"
        if corruption == "schema":
            value = json.loads(record.read_text(encoding="utf-8"))
            value["schema_version"] = 999
            record.write_text(json.dumps(value), encoding="utf-8")
        else:
            record.write_bytes(b'{\r\n"value":\r\n}' if corruption == "crlf-json" else b'{"value":"\xff"}')
    arguments = ["writer-session", mode]
    if mode == "check":
        arguments += [holder.client_id, str(path), str(holder.pid), "--session-id", holder.session_id]
    else:
        arguments += [str(path)]
    arguments += ["--access", "read"]
    before = metadata(repository)
    with decode_cache():
        former_exit = cached_controller_main(arguments)
    former = json.loads(capsys.readouterr().out)
    assert controller_main(arguments) == former_exit == (2 if corruption else 0)
    assert json.loads(capsys.readouterr().out) == former
    assert former["decision"]["code"] == ("INVALID_METADATA" if corruption else "HOST_HANDOFF_CONTINUATION")
    assert metadata(repository) == before


@pytest.mark.parametrize("fixture_name", ["hosted", "frozen"])
def test_direct_decision_serializes_concurrent_lease_revocation(request, monkeypatch, fixture_name):
    repository, path, _, holder, *_ = request.getfixturevalue(fixture_name)
    store = _store(repository)
    probed, revoked = Event(), Event()
    blocked, errors = [], []

    def revoke():
        try:
            # A separate open description observes the real flock; no sleeps or
            # mocked locks can make an unlocked check look serialized.
            with store.lock_file.open("rb") as stream:
                try:
                    fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    blocked.append(True)
                else:
                    blocked.append(False)
                    fcntl.flock(stream, fcntl.LOCK_UN)
            probed.set()
            writer_lease.release_writer_lease(repository.repo, target=path, holder=holder)
        except BaseException as exc:
            errors.append(exc)
            probed.set()
        finally:
            revoked.set()

    worker = Thread(target=revoke, daemon=True)
    guard, continuation = controller.guard_write, writer_session._continuation

    def guarded(*args, **kwargs):
        worker.start()
        assert probed.wait(10), "lease revocation did not reach its lock probe"
        assert blocked == [True], "lease could change during the entitlement decision"
        assert not revoked.is_set()
        return guard(*args, **kwargs)

    def continued(*args, **kwargs):
        # The read lock must still cover the continuation, not just the guard.
        with store.lock_file.open("rb") as stream, pytest.raises(BlockingIOError):
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert not revoked.is_set()
        return continuation(*args, **kwargs)

    monkeypatch.setattr(controller, "guard_write", guarded)
    monkeypatch.setattr(writer_session, "_continuation", continued)
    try:
        decision = writer_session.check(path, client_id=holder.client_id, selector=str(holder.pid),
            session_id=holder.session_id, run_id=holder.run_id, access="read")
    finally:
        if worker.ident is not None:
            worker.join(timeout=10)
    assert not worker.is_alive() and revoked.is_set() and not errors
    assert decision["admission"] == ("GRANTED" if fixture_name == "hosted" else "CONTINUATION_GRANTED")
    monkeypatch.setattr(controller, "guard_write", guard)
    monkeypatch.setattr(writer_session, "_continuation", continuation)
    after = writer_session.check(path, client_id=holder.client_id, selector=str(holder.pid),
        session_id=holder.session_id, run_id=holder.run_id, access="read")
    assert after["admission"] == "REFUSED"
    assert after["code"] == ("BLOCK_WRITER_LEASE_REQUIRED" if fixture_name == "hosted" else "WRITER_LEASE_REQUIRED")


@pytest.mark.parametrize("fault,code", [
    ("missing-lease", "WRITER_LEASE_REQUIRED"),
    ("expired-lease", "WRITER_LEASE_MISMATCH"),
    ("foreign-session", "WRITER_LEASE_MISMATCH"),
    ("head-drift", "RECOVERY_REQUIRED"),
    ("malformed-metadata", "INVALID_METADATA"),
])
def test_direct_continuation_refuses_current_authority_faults(frozen, monkeypatch, fault, code):
    repository, path, _, holder = frozen
    session = holder.session_id
    if fault == "missing-lease":
        writer_lease.release_writer_lease(repository.repo, target=path, holder=holder)
    elif fault == "expired-lease":
        monkeypatch.setattr(writer_lease, "lease_is_expired", lambda lease: True)
    elif fault == "foreign-session":
        session = "another-session"
    elif fault == "head-drift":
        git(path, "commit", "--allow-empty", "-m", "drift after handoff")
    else:
        _store(repository).repo_file.write_text("{", encoding="utf-8")
    before = metadata(repository)
    decision = writer_session.check(path, client_id=holder.client_id, selector=str(holder.pid),
        session_id=session, run_id=holder.run_id, access="read")
    assert decision["admission"] == "REFUSED" and decision["code"] == code
    assert metadata(repository) == before
