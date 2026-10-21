"""Handoff scope evidence, direct-check continuation and cheap host checks."""

from __future__ import annotations

import json
import os

import pytest

from tests.test_controlled import _configure_remote, acquire_one, adopt_canonical
from tests.test_integration_pipeline import POLICY_THREE_CANDIDATES, _enqueue
from tests.test_shadow import GitRepository, git
from worktree_controller import controller, writer_lease, writer_session
from worktree_controller.cli import main as controller_main
from worktree_controller.lifecycle import handoff
from worktree_controller.models import ControllerError


@pytest.fixture
def repository():
    with GitRepository(linked=0, policy_text=POLICY_THREE_CANDIDATES) as repository:
        adopt_canonical(repository)
        _configure_remote(repository)
        yield repository


def _changed_task(repository, task="unscoped", files=("a.txt", "docs/b.md")):
    path, payload = acquire_one(repository, task=task, authority=task)
    for name in files:
        (path / name).parent.mkdir(parents=True, exist_ok=True)
        (path / name).write_text(f"{name}\n", encoding="utf-8")
    if files:
        git(path, "add", *files)
        git(path, "commit", "-m", f"change {task}")
    return path, str(payload["decision"]["allocation_id"])


def _handoff(repository, path, expected=1, **options):
    return handoff(
        repository.repo,
        target=path,
        expected_record_version=expected,
        kind="integration-ready",
        commit=git(path, "rev-parse", "HEAD"),
        finalizer_record_id="test-finalizer-record",
        **options,
    )


def _record(repository, allocation):
    from worktree_controller.git_backend import inventory
    from worktree_controller.store import MetadataStore

    return MetadataStore(inventory(repository.repo).common_dir).load().records[allocation]


def _scope_warnings(payload):
    return [w for w in payload["warnings"] if w["code"] == "MISSING_HANDOFF_SCOPE"]


def test_unscoped_changed_handoff_warns_before_enqueue_refuses(repository):
    path, allocation = _changed_task(repository)
    payload = _handoff(repository, path)
    assert payload["decision"]["code"] == "HANDOFF_READY"
    (warning,) = _scope_warnings(payload)
    assert warning["allocation_id"] == allocation
    assert "--scope-changed" in warning["next_action"]
    with pytest.raises(ControllerError) as refused:
        _enqueue(repository, task="unscoped-integration", source_allocation=allocation)
    assert refused.value.code == "MISSING_SOURCE_SCOPE"


def test_scope_changed_records_exact_paths_and_enqueues(repository):
    path, allocation = _changed_task(repository)
    payload = _handoff(repository, path, scope_changed=True)
    assert not _scope_warnings(payload)
    assert _record(repository, allocation).scope_paths == ("a.txt", "docs/b.md")
    queued = _enqueue(repository, task="scoped-integration", source_allocation=allocation)
    assert queued["decision"]["code"] == "INTEGRATION_ENQUEUED"
    assert queued["decision"]["scope_paths"] == ["a.txt", "docs/b.md"]


def test_scope_changed_repeats_a_scopeless_frozen_handoff(repository):
    path, allocation = _changed_task(repository)
    _handoff(repository, path)
    repeated = _handoff(repository, path, expected=2, scope_changed=True)
    assert repeated["decision"]["record_version"] == 3
    assert not _scope_warnings(repeated)
    assert _record(repository, allocation).scope_paths == ("a.txt", "docs/b.md")


def test_explicit_scope_and_no_change_handoffs_have_no_warning(repository):
    path, _ = _changed_task(repository)
    assert not _scope_warnings(_handoff(repository, path, scope_paths=("a.txt", "docs")))
    unchanged, _ = _changed_task(repository, task="unchanged", files=())
    assert not _scope_warnings(_handoff(repository, unchanged))


@pytest.mark.parametrize("shape", ["no-change", "with-scope", "already-scoped"])
def test_scope_changed_refuses_ambiguous_requests(repository, shape):
    if shape == "no-change":
        path, _ = _changed_task(repository, files=())
        options, code = {"scope_changed": True}, "MISSING_HANDOFF_SCOPE"
    elif shape == "with-scope":
        path, _ = _changed_task(repository)
        options, code = {"scope_changed": True, "scope_paths": ("a.txt",)}, "SCOPE_MISMATCH"
    else:
        path, _ = acquire_one(repository, task="scoped", authority="scoped", scope_paths=("a.txt",))
        (path / "a.txt").write_text("a\n", encoding="utf-8")
        git(path, "add", "a.txt")
        git(path, "commit", "-m", "scoped")
        options, code = {"scope_changed": True}, "SCOPE_MISMATCH"
    with pytest.raises(ControllerError) as refused:
        _handoff(repository, path, **options)
    assert refused.value.code == code


def test_cli_handoff_accepts_scope_changed(repository, capsys):
    path, allocation = _changed_task(repository)
    code = controller_main([
        "handoff", "--repo", str(repository.repo), "--path", str(path),
        "--expected-record-version", "1", "--kind", "integration-ready",
        "--commit", git(path, "rev-parse", "HEAD"), "--finalizer-record-id", "record",
        "--scope-changed", "--json",
    ])
    assert code == 0, capsys.readouterr().out
    assert _record(repository, allocation).scope_paths == ("a.txt", "docs/b.md")


def _lease(repository, path, session):
    holder = writer_lease.resolve_holder(
        str(os.getpid()), client_id="direct-client", session_id=session, run_id=None
    )
    writer_lease.acquire_writer_lease(
        repository.repo, target=path, client_id=holder.client_id, holder_pid=holder.pid,
        session_id=holder.session_id, run_id=None, holder=holder,
    )


@pytest.fixture
def direct(repository, monkeypatch):
    monkeypatch.delenv("CONTROLLER_WRITER_MODE", raising=False)
    monkeypatch.delenv("CONTROLLER_WRITER_CONTEXT", raising=False)
    path, allocation = _changed_task(repository, task="direct", files=("direct.txt",))
    _lease(repository, path, "direct-session")
    identity = {"client_id": "direct-client", "selector": str(os.getpid()),
                "session_id": "direct-session"}
    assert writer_session.check(path, **identity)["admission"] == "GRANTED"
    _handoff(repository, path, scope_changed=True)
    return repository, path, allocation, identity


def test_direct_check_keeps_the_frozen_fence_and_continues_the_holder(direct, tmp_path):
    _repository, path, allocation, identity = direct
    assert writer_session.check(path, **identity)["code"] == "BLOCK_FEATURE_WRITE_LIFECYCLE"
    for access in ("read", "exec"):
        decision = writer_session.check(path, **identity, access=access)
        assert decision["admission"] == "CONTINUATION_GRANTED"
        assert decision["allocation_id"] == allocation
    frozen = writer_session.check(path, **identity, access="write", target=path / "direct.txt")
    assert frozen["admission"] == "REFUSED"
    outside = writer_session.check(path, **identity, access="write", target=tmp_path / "x")
    assert outside["admission"] == "CONTINUATION_GRANTED"


def test_direct_check_of_another_holder_is_refused(direct):
    _repository, path, _, identity = direct
    other = {**identity, "session_id": "another-session"}
    assert writer_session.check(path, **other, access="read")["admission"] == "REFUSED"


def test_frozen_holder_writes_into_another_checkout_it_holds(direct):
    repository, path, _, identity = direct
    held, held_allocation = _changed_task(repository, task="held", files=())
    _lease(repository, held, "held-session")
    granted = writer_session.check(path, **identity, access="write", target=held / "new.txt")
    assert granted["admission"] == "CONTINUATION_GRANTED"
    assert granted["target_allocation_id"] == held_allocation
    unheld, _ = _changed_task(repository, task="unheld", files=())
    refused = writer_session.check(path, **identity, access="write", target=unheld / "new.txt")
    assert refused["admission"] == "REFUSED"


def test_direct_check_cli_exit_status(direct, capsys):
    _repository, path, _, identity = direct
    base = ["writer-session", "check", "direct-client", str(path), identity["selector"],
            "--session-id", "direct-session"]
    assert controller_main([*base, "--access", "read"]) == 0
    assert json.loads(capsys.readouterr().out)["decision"]["admission"] == "CONTINUATION_GRANTED"
    assert controller_main(base) == 2


def test_entitlement_checks_skip_the_full_status_scan(direct, monkeypatch):
    _repository, path, _, identity = direct

    def forbidden(*args, **kwargs):
        raise AssertionError("status() must not run for an entitlement check")

    monkeypatch.setattr(controller, "status", forbidden)
    decision = writer_session.check(path, **identity, access="read")
    assert decision["admission"] == "CONTINUATION_GRANTED"


def test_capabilities_advertise_direct_check_continuation(capsys):
    assert controller_main(["capabilities", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["decision"]["host_handoff_continuation_version"] == 2
